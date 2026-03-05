from __future__ import annotations

import csv
import io
import os
from datetime import datetime, date, time as dt_time, timedelta, timezone
from flask import Flask, render_template, redirect, url_for, request, flash, send_file, jsonify, abort
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash

from db_bootstrap import initialize_database
from models import db, User, Approval, AuditLog, DailyWorkStat, BreakRequest, Device


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "change-me"
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("TIMETRACK_DB_URI", "sqlite:///time_tracking.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    with app.app_context():
        initialize_database(reset_existing_users=False)

    login_manager = LoginManager()
    login_manager.login_view = "login"
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id: str):
        try:
            return db.session.get(User, int(user_id))
        except (TypeError, ValueError):
            return None

    try:
        from zoneinfo import ZoneInfo
    except Exception:
        ZoneInfo = None

    def _org_tzinfo():
        name = (os.environ.get("TIMETRACK_TZ") or "").strip()
        if name and ZoneInfo:
            try:
                return ZoneInfo(name)
            except Exception:
                pass
        return datetime.now().astimezone().tzinfo or timezone.utc

    ORG_TZ = _org_tzinfo()

    def utcnow() -> datetime:
        return datetime.utcnow()

    def safe_int(value, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
        try:
            parsed = int(str(value).strip())
        except (AttributeError, TypeError, ValueError):
            parsed = default

        if minimum is not None:
            parsed = max(minimum, parsed)
        if maximum is not None:
            parsed = min(maximum, parsed)
        return parsed

    def stale_limit_seconds() -> int:
        return safe_int(os.environ.get("TIMETRACK_STALE_LIMIT"), 120, minimum=1)

    def device_alive_seconds() -> int:
        return safe_int(os.environ.get("TIMETRACK_DEVICE_ALIVE"), 60, minimum=1)

    def live_display_window_seconds() -> int:
        return min(
            stale_limit_seconds(),
            safe_int(os.environ.get("TIMETRACK_LIVE_WINDOW"), 30, minimum=1),
        )

    def local_date_from_utc(dt_utc: datetime) -> date:
        return dt_utc.replace(tzinfo=timezone.utc).astimezone(ORG_TZ).date()

    def local_midnight_utc(local_d: date) -> datetime:
        aware = datetime.combine(local_d, dt_time(0, 0)).replace(tzinfo=ORG_TZ)
        return aware.astimezone(timezone.utc).replace(tzinfo=None)

    def local_date_range_utc(start_local: date, end_local: date) -> tuple[datetime, datetime]:
        return local_midnight_utc(start_local), local_midnight_utc(end_local + timedelta(days=1))

    def localize_utc(dt_utc: datetime | None) -> datetime | None:
        if dt_utc is None:
            return None
        return dt_utc.replace(tzinfo=timezone.utc).astimezone(ORG_TZ)

    def format_local_datetime(dt_utc: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
        localized = localize_utc(dt_utc)
        return localized.strftime(fmt) if localized else ""

    def format_local_time(value: datetime | dt_time | None, fmt: str = "%H:%M") -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            localized = localize_utc(value)
            return localized.strftime(fmt) if localized else ""
        return value.strftime(fmt)

    app.jinja_env.filters["localdt"] = format_local_datetime
    app.jinja_env.filters["localtime"] = format_local_time

    def period_from_request(default_day: date) -> tuple[int, int]:
        year = safe_int(request.args.get("year"), default_day.year, minimum=2000, maximum=2100)
        month = safe_int(request.args.get("month"), default_day.month, minimum=1, maximum=12)
        return year, month

    def has_live_device(user_id: int, now_utc: datetime) -> bool:
        alive_sec = device_alive_seconds()
        cutoff = now_utc - timedelta(seconds=alive_sec)
        return (
            Device.query.filter(
                Device.user_id == user_id,
                Device.enabled.is_(True),
                Device.last_seen_at.isnot(None),
                Device.last_seen_at >= cutoff,
            ).first()
            is not None
        )


    def period_bounds(year: int, month: int):
        start = date(year, month, 1)
        if month == 12:
            end = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(year, month + 1, 1) - timedelta(days=1)
        return start, end

    def format_hms(seconds: int):
        seconds = max(0, int(seconds))
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return h, m, s

    def break_overlap_seconds(user_id: int, start_utc: datetime, end_utc: datetime) -> int:
        if end_utc <= start_utc:
            return 0

        overlaps = (
            BreakRequest.query.filter(
                BreakRequest.user_id == user_id,
                BreakRequest.status.in_(("PENDING", "APPROVED")),
                BreakRequest.end_at > start_utc,
                BreakRequest.start_at < end_utc,
            )
            .order_by(BreakRequest.start_at.asc(), BreakRequest.end_at.asc())
            .all()
        )

        merged: list[list[datetime]] = []
        for br in overlaps:
            start_at = max(start_utc, br.start_at)
            end_at = min(end_utc, br.end_at)
            if end_at <= start_at:
                continue

            if not merged or start_at >= merged[-1][1]:
                merged.append([start_at, end_at])
                continue

            if end_at > merged[-1][1]:
                merged[-1][1] = end_at

        total = 0.0
        for start_at, end_at in merged:
            total += (end_at - start_at).total_seconds()

        return max(0, int(total))

    def active_interval_seconds(user_id: int, start_utc: datetime, end_utc: datetime) -> int:
        if end_utc <= start_utc:
            return 0

        total_seconds = int((end_utc - start_utc).total_seconds())
        return max(0, total_seconds - break_overlap_seconds(user_id, start_utc, end_utc))

    def display_active_seconds(stat: DailyWorkStat | None, user_id: int, now_utc: datetime) -> int:
        seconds = stat.active_seconds if stat else 0
        if (stat is None) or (stat.last_ping_at is None) or (not stat.last_state_active):
            return max(0, int(seconds))
        display_end = min(now_utc, stat.last_ping_at + timedelta(seconds=live_display_window_seconds()))
        return max(0, int(seconds) + active_interval_seconds(user_id, stat.last_ping_at, display_end))

    def is_user_active_now(stat: DailyWorkStat | None, user_id: int, now_utc: datetime) -> bool:
        if (stat is None) or (stat.last_ping_at is None) or (not stat.last_state_active):
            return False
        if is_on_break(user_id, now_utc):
            return False

        delta = (now_utc - stat.last_ping_at).total_seconds()
        return 0 <= delta <= live_display_window_seconds()

    def manageable_employees_query():
        if current_user.role == "admin":
            return User.query.filter(User.role == "employee")
        return User.query.filter_by(department_id=current_user.department_id, role="employee")


    def is_on_break(user_id: int, at: datetime) -> bool:
        return (
            BreakRequest.query.filter(
                BreakRequest.user_id == user_id,
                BreakRequest.status.in_(("PENDING", "APPROVED")),
                BreakRequest.start_at <= at,
                BreakRequest.end_at >= at,
            ).first()
            is not None
        )

    def add_active_seconds(user_id: int, is_active: bool, now_utc: datetime):
        work_date = local_date_from_utc(now_utc)
        stat = DailyWorkStat.query.filter_by(user_id=user_id, work_date=work_date).first()

        if stat is None:
            stat = DailyWorkStat(
                user_id=user_id,
                work_date=work_date,
                active_seconds=0,
                last_ping_at=None,
                last_state_active=False,
            )
            db.session.add(stat)

        stale_limit = stale_limit_seconds()

        if stat.last_ping_at is None:
            prev_date = work_date - timedelta(days=1)
            prev = DailyWorkStat.query.filter_by(user_id=user_id, work_date=prev_date).first()

            if prev and prev.last_ping_at:
                total = (now_utc - prev.last_ping_at).total_seconds()
                if 0 <= total <= stale_limit:
                    midnight_utc = local_midnight_utc(work_date)
                    if prev.last_ping_at < midnight_utc <= now_utc:
                        if prev.last_state_active:
                            prev.active_seconds += active_interval_seconds(user_id, prev.last_ping_at, midnight_utc)
                            stat.active_seconds += active_interval_seconds(user_id, midnight_utc, now_utc)

            stat.last_ping_at = now_utc
            stat.last_state_active = is_active
            db.session.commit()
            return stat

        delta = (now_utc - stat.last_ping_at).total_seconds()
        if delta < 0:
            delta = 0

        if delta > stale_limit:
            stat.last_ping_at = now_utc
            stat.last_state_active = is_active
            db.session.commit()
            return stat

        if stat.last_state_active:
            stat.active_seconds += active_interval_seconds(user_id, stat.last_ping_at, now_utc)

        stat.last_ping_at = now_utc
        stat.last_state_active = is_active
        db.session.commit()
        return stat



    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = User.query.filter_by(username=username).first()
            if (not user) or (not check_password_hash(user.password_hash, password)):
                flash("Неверный логин или пароль", "danger")
                return redirect(url_for("login"))
            login_user(user)
            AuditLog.log(user.id, "Вход")
            return redirect(url_for("dashboard"))
        return render_template("login.html")

    @app.route("/logout")
    @login_required
    def logout():
        AuditLog.log(current_user.id, "Выход")
        logout_user()
        return redirect(url_for("login"))

    @app.route("/dashboard")
    @login_required
    def dashboard():
        now_utc = utcnow()
        today = local_date_from_utc(now_utc)
        start_week = today - timedelta(days=today.weekday())
        end_week = start_week + timedelta(days=6)

        stats = (
            DailyWorkStat.query.filter(
                DailyWorkStat.user_id == current_user.id,
                DailyWorkStat.work_date >= start_week,
                DailyWorkStat.work_date <= end_week,
            )
            .order_by(DailyWorkStat.work_date.asc())
            .all()
        )
        stat_map = {s.work_date: s for s in stats}

        days = []
        total_active_seconds = 0
        today_active_seconds = 0
        for i in range(7):
            d = start_week + timedelta(days=i)
            s = stat_map.get(d)
            active_seconds = display_active_seconds(s, current_user.id, now_utc) if d == today else (s.active_seconds if s else 0)
            if d == today:
                today_active_seconds = active_seconds
            total_active_seconds += active_seconds
            days.append({"date": d, "active_seconds": active_seconds})

        on_break = is_on_break(current_user.id, now_utc)
        today_stat = stat_map.get(today)
        is_active_now = is_user_active_now(today_stat, current_user.id, now_utc)

        recent_breaks = (
            BreakRequest.query.filter_by(user_id=current_user.id)
            .order_by(BreakRequest.requested_at.desc())
            .limit(10)
            .all()
        )

        return render_template(
            "dashboard.html",
            days=days,
            total_active_seconds=total_active_seconds,
            today_active_seconds=today_active_seconds,
            today_iso=today.isoformat(),
            start=start_week,
            end=end_week,
            is_active_now=is_active_now,
            on_break=on_break,
            recent_breaks=recent_breaks,
        )

    @app.route("/api/ping", methods=["POST"])
    @login_required
    def api_ping():
        payload = request.get_json(silent=True) or {}
        is_active = bool(payload.get("active", False))
        now_utc = utcnow()

        if has_live_device(current_user.id, now_utc):
            today = local_date_from_utc(now_utc)
            stat = DailyWorkStat.query.filter_by(user_id=current_user.id, work_date=today).first()
            seconds = display_active_seconds(stat, current_user.id, now_utc)
            h, m, s = format_hms(seconds)
            return jsonify(
                {
                    "active_h": h,
                    "active_m": m,
                    "active_s": s,
                    "is_active": is_user_active_now(stat, current_user.id, now_utc),
                    "source": "agent",
                }
            )

        stat = add_active_seconds(current_user.id, is_active, now_utc)
        seconds = display_active_seconds(stat, current_user.id, now_utc)
        h, m, s = format_hms(seconds)
        return jsonify(
            {
                "active_h": h,
                "active_m": m,
                "active_s": s,
                "is_active": is_user_active_now(stat, current_user.id, now_utc),
                "source": "web",
            }
        )


    @app.route("/api/me/live", methods=["GET"])
    @login_required
    def api_me_live():
        now_utc = utcnow()
        today = local_date_from_utc(now_utc)
        stat = DailyWorkStat.query.filter_by(user_id=current_user.id, work_date=today).first()
        active_seconds = display_active_seconds(stat, current_user.id, now_utc)
        is_active_now = is_user_active_now(stat, current_user.id, now_utc)
        on_break = is_on_break(current_user.id, now_utc)
        return jsonify({
            "date": today.isoformat(),
            "active_seconds": active_seconds,
            "total_seconds": active_seconds,
            "is_active_now": is_active_now,
            "on_break": on_break
        })



    @app.route("/api/agent/handshake", methods=["POST"])
    def agent_handshake():
        payload = request.get_json(silent=True) or {}
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        device_hash = str(payload.get("device_hash", "")).strip()

        if not username or not password or not device_hash:
            abort(400)

        user = User.query.filter_by(username=username).first()
        if (not user) or (not check_password_hash(user.password_hash, password)):
            abort(401)

        dev = Device.query.filter_by(user_id=user.id, device_hash=device_hash).first()
        if not dev:
            dev = Device.create(user_id=user.id, device_hash=device_hash)
            db.session.add(dev)
            db.session.commit()

        if not dev.enabled:
            abort(403)

        dev.last_seen_at = datetime.utcnow()
        db.session.commit()

        return jsonify({"token": dev.token})

    @app.route("/api/agent/ping", methods=["POST"])
    def agent_ping():
        token = request.headers.get("X-Device-Token", "").strip()
        device_hash = request.headers.get("X-Device-Hash", "").strip()
        if not token or not device_hash:
            abort(401)

        dev = Device.query.filter_by(token=token).first()
        if (not dev) or (not dev.enabled) or (dev.device_hash != device_hash):
            abort(401)

        payload = request.get_json(silent=True) or {}
        is_active = bool(payload.get("active", False))
        add_active_seconds(dev.user_id, is_active, utcnow())

        dev.last_seen_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"ok": True})

    @app.route("/devices", methods=["GET", "POST"])
    @login_required
    def devices():
        if current_user.role != "admin":
            flash("Нет доступа", "warning")
            return redirect(url_for("dashboard"))

        if request.method == "POST":
            dev_id = safe_int(request.form.get("device_id"), 0, minimum=0)
            action = request.form.get("action", "")
            if dev_id <= 0 or action not in {"disable", "enable"}:
                flash("Некорректный запрос", "warning")
                return redirect(url_for("devices"))

            dev = Device.query.get_or_404(dev_id)
            if action == "disable":
                dev.enabled = False
                AuditLog.log(current_user.id, f"Отключено устройство {dev.id}", commit=False)
            else:
                dev.enabled = True
                AuditLog.log(current_user.id, f"Включено устройство {dev.id}", commit=False)
            db.session.commit()
            return redirect(url_for("devices"))

        items = Device.query.order_by(Device.created_at.desc()).limit(300).all()
        users = {u.id: u for u in User.query.all()}
        return render_template("devices.html", items=items, users=users)

    @app.route("/breaks", methods=["GET", "POST"])
    @login_required
    def breaks():
        if request.method == "POST":
            action = request.form.get("action", "")
            if action == "request_break":
                minutes = safe_int(request.form.get("minutes"), 15, minimum=5, maximum=180)
                now = utcnow()
                existing_break = (
                    BreakRequest.query.filter(
                        BreakRequest.user_id == current_user.id,
                        BreakRequest.status.in_(("PENDING", "APPROVED")),
                        BreakRequest.end_at > now,
                    )
                    .order_by(BreakRequest.end_at.desc())
                    .first()
                )
                if existing_break is not None:
                    flash("У вас уже есть активная или ожидающая заявка на перерыв", "warning")
                    return redirect(url_for("breaks"))

                br = BreakRequest(
                    user_id=current_user.id,
                    start_at=now,
                    end_at=now + timedelta(minutes=minutes),
                    status="PENDING",
                )
                db.session.add(br)
                AuditLog.log(current_user.id, f"Перерыв {minutes} мин", commit=False)
                db.session.commit()
                flash("Заявка отправлена", "success")
                return redirect(url_for("breaks"))

            if current_user.role != "admin":
                flash("Нет доступа", "danger")
                return redirect(url_for("breaks"))

            br_id = safe_int(request.form.get("break_id"), 0, minimum=0)
            if br_id <= 0 or action not in {"approve", "reject"}:
                flash("Некорректный запрос", "warning")
                return redirect(url_for("breaks"))

            br = BreakRequest.query.get_or_404(br_id)

            if action == "approve":
                br.status = "APPROVED"
                br.approved_by = current_user.id
                br.approved_at = datetime.utcnow()
                AuditLog.log(current_user.id, f"Утвержден перерыв {br.id}", commit=False)
                flash("Утверждено", "success")
            else:
                br.status = "REJECTED"
                br.approved_by = current_user.id
                br.approved_at = datetime.utcnow()
                AuditLog.log(current_user.id, f"Отклонен перерыв {br.id}", commit=False)
                flash("Отклонено", "warning")

            db.session.commit()
            return redirect(url_for("breaks"))

        if current_user.role == "admin":
            items = BreakRequest.query.order_by(BreakRequest.requested_at.desc()).limit(200).all()
        else:
            items = (
                BreakRequest.query.filter_by(user_id=current_user.id)
                .order_by(BreakRequest.requested_at.desc())
                .limit(200)
                .all()
            )
        return render_template("breaks.html", items=items)

    @app.route("/approvals", methods=["GET", "POST"])
    @login_required
    def approvals():
        if current_user.role not in ("manager", "admin"):
            flash("Нет доступа", "warning")
            return redirect(url_for("dashboard"))

        today = local_date_from_utc(utcnow())
        year, month = period_from_request(today)
        start, end = period_bounds(year, month)

        if request.method == "POST":
            user_id = safe_int(request.form.get("user_id"), 0, minimum=0)
            action = request.form.get("action")
            allowed_ids = {row[0] for row in manageable_employees_query().with_entities(User.id).all()}
            if user_id not in allowed_ids or action not in {"approve", "reject"}:
                flash("Некорректный запрос", "warning")
                return redirect(url_for("approvals", year=year, month=month))

            appr = Approval.query.filter_by(user_id=user_id, year=year, month=month).first()
            if not appr:
                appr = Approval(user_id=user_id, year=year, month=month, status="DRAFT")
                db.session.add(appr)

            if action == "approve":
                appr.status = "APPROVED"
                appr.approved_by = current_user.id
                appr.approved_at = datetime.utcnow()
                AuditLog.log(current_user.id, f"Табель утвержден {user_id} {month:02d}.{year}", commit=False)
                flash("Табель утвержден", "success")
            else:
                appr.status = "DRAFT"
                appr.approved_by = None
                appr.approved_at = None
                AuditLog.log(current_user.id, f"Табель сброшен {user_id} {month:02d}.{year}", commit=False)
                flash("Статус сброшен", "info")

            db.session.commit()
            return redirect(url_for("approvals", year=year, month=month))

        employees = manageable_employees_query().order_by(User.last_name).all()

        summaries = []
        for emp in employees:
            stats = DailyWorkStat.query.filter(
                DailyWorkStat.user_id == emp.id,
                DailyWorkStat.work_date >= start,
                DailyWorkStat.work_date <= end,
            ).all()
            total_minutes = int(sum(s.active_seconds for s in stats) // 60)
            appr = Approval.query.filter_by(user_id=emp.id, year=year, month=month).first()
            summaries.append(
                {"employee": emp, "total_minutes": total_minutes, "status": appr.status if appr else "DRAFT"}
            )

        return render_template("approvals.html", summaries=summaries, year=year, month=month, start=start, end=end)

    @app.route("/reports")
    @login_required
    def reports():
        today = local_date_from_utc(utcnow())
        year, month = period_from_request(today)
        user_id = request.args.get("user_id")
        start, end = period_bounds(year, month)

        if current_user.role == "admin":
            selectable_users = User.query.order_by(User.last_name).all()
        elif current_user.role == "manager":
            selectable_users = (
                User.query.filter((User.department_id == current_user.department_id) | (User.id == current_user.id))
                .order_by(User.last_name)
                .all()
            )
        else:
            selectable_users = [current_user]

        user_id = safe_int(user_id, current_user.id, minimum=1)
        allowed_ids = {u.id for u in selectable_users}
        if user_id not in allowed_ids:
            flash("Нет доступа", "danger")
            return redirect(url_for("reports"))

        selected_user = db.session.get(User, user_id)
        if selected_user is None:
            abort(404)

        daily = (
            DailyWorkStat.query.filter(
                DailyWorkStat.user_id == user_id,
                DailyWorkStat.work_date >= start,
                DailyWorkStat.work_date <= end,
            )
            .order_by(DailyWorkStat.work_date.asc())
            .all()
        )
        total_minutes = int(sum(d.active_seconds for d in daily) // 60)
        breaks_start_utc, breaks_end_utc = local_date_range_utc(start, end)

        br = (
            BreakRequest.query.filter(
                BreakRequest.user_id == user_id,
                BreakRequest.end_at > breaks_start_utc,
                BreakRequest.start_at < breaks_end_utc,
            )
            .order_by(BreakRequest.start_at.asc())
            .all()
        )

        return render_template(
            "reports.html",
            daily=daily,
            breaks=br,
            total_minutes=total_minutes,
            year=year,
            month=month,
            start=start,
            end=end,
            selectable_users=selectable_users,
            selected_user=selected_user,
        )

    @app.route("/reports/export.csv")
    @login_required
    def export_csv():
        today = local_date_from_utc(utcnow())
        year, month = period_from_request(today)
        user_id = safe_int(request.args.get("user_id"), current_user.id, minimum=1)
        start, end = period_bounds(year, month)

        if current_user.role == "admin":
            allowed_ids = {u.id for u in User.query.all()}
        elif current_user.role == "manager":
            allowed_ids = {
                u.id
                for u in User.query.filter(
                    (User.department_id == current_user.department_id) | (User.id == current_user.id)
                ).all()
            }
        else:
            allowed_ids = {current_user.id}

        if user_id not in allowed_ids:
            flash("Нет доступа", "danger")
            return redirect(url_for("reports"))

        user = db.session.get(User, user_id)
        if user is None:
            abort(404)

        daily = (
            DailyWorkStat.query.filter(
                DailyWorkStat.user_id == user_id,
                DailyWorkStat.work_date >= start,
                DailyWorkStat.work_date <= end,
            )
            .order_by(DailyWorkStat.work_date.asc())
            .all()
        )

        output = io.StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["Сотрудник", "Дата", "Активное время (ч)", "Активное время (мин)"])
        for d in daily:
            h, m, s = format_hms(d.active_seconds)
            writer.writerow([f"{user.last_name} {user.first_name}", d.work_date.isoformat(), h, m])

        mem = io.BytesIO(output.getvalue().encode("utf-8-sig"))
        filename = f"activity_{user.username}_{year}_{month:02d}.csv"
        return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=filename)

    @app.route("/audit")
    @login_required
    def audit():
        if current_user.role != "admin":
            flash("Нет доступа", "warning")
            return redirect(url_for("dashboard"))
        logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(200).all()
        return render_template("audit.html", logs=logs)

    return app

import sys
import time
import socket
import hashlib
import getpass
import threading
import subprocess
import importlib.util
from datetime import datetime as _dt


def _safe_env_int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        value = default

    if minimum is not None:
        value = max(minimum, value)
    return value


def _agent_data_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    p = os.path.join(base, "TimeTrack")
    os.makedirs(p, exist_ok=True)
    return p


def _agent_log_path() -> str:
    return os.path.join(_agent_data_dir(), "agent.log")


def _agent_log_line(msg: str) -> None:
    try:
        with open(_agent_log_path(), "a", encoding="utf-8") as f:
            f.write(f"{_dt.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _agent_ensure_deps() -> None:
    need = {
        "requests": "requests",
        "pynput": "pynput",
        "pystray": "pystray",
        "PIL": "pillow",
    }
    missing = []
    for mod, pip_name in need.items():
        if importlib.util.find_spec(mod) is None:
            missing.append(pip_name)

    if not missing:
        return

    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"] + missing
    try:
        subprocess.check_call(cmd)
    except Exception as e:
        _agent_log_line(f"pip install failed: {e}")
        raise


def _agent_machine_guid() -> str:
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
        val, _ = winreg.QueryValueEx(key, "MachineGuid")
        return str(val)
    except Exception:
        return "noguid"


def _agent_device_hash() -> str:
    user = os.environ.get("USERNAME", "")
    raw = (_agent_machine_guid() + "|" + user).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _agent_token_path() -> str:
    return os.path.join(_agent_data_dir(), "device.dat")


def _agent_load_token() -> str:
    p = _agent_token_path()
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""
    return ""


def _agent_save_token(t: str) -> None:
    with open(_agent_token_path(), "w", encoding="utf-8") as f:
        f.write(t)


def _agent_clear_token() -> None:
    p = _agent_token_path()
    try:
        if os.path.exists(p):
            os.remove(p)
    except Exception as e:
        _agent_log_line(f"token clear failed: {e}")


def _probe_server_host(host: str) -> str:
    host = (host or "").strip()
    if host in {"", "0.0.0.0"}:
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def _wait_for_server(host: str, port: int, timeout_seconds: int = 15) -> bool:
    deadline = time.time() + max(1, timeout_seconds)
    probe_host = _probe_server_host(host)

    while time.time() < deadline:
        try:
            with socket.create_connection((probe_host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)

    return False


class _AgentActivityState:
    def __init__(self):
        self.last = time.time()
        self.lock = threading.Lock()

    def bump(self):
        with self.lock:
            self.last = time.time()

    def active(self, idle_seconds: int) -> bool:
        with self.lock:
            return (time.time() - self.last) < idle_seconds


def _agent_handshake(base_url: str, requests_mod):
    username = (os.environ.get("TIMETRACK_USERNAME") or "").strip()
    password = os.environ.get("TIMETRACK_PASSWORD") or ""

    if not username or not password:
        username = input("Логин: ").strip()
        password = getpass.getpass("Пароль: ")

    payload = {"username": username, "password": password, "device_hash": _agent_device_hash()}
    r = requests_mod.post(base_url.rstrip("/") + "/api/agent/handshake", json=payload, timeout=10)
    r.raise_for_status()
    t = (r.json() or {}).get("token", "")
    t = (t or "").strip()
    if not t:
        raise RuntimeError("Handshake failed")
    _agent_save_token(t)
    return t


def run_embedded_agent() -> None:
    if not sys.platform.startswith("win"):
        print("Embedded agent: пропуск (агент рассчитан на Windows GUI).")
        return

    _agent_ensure_deps()

    import requests
    from pynput import mouse, keyboard
    import pystray
    from pystray import MenuItem as item
    from PIL import Image, ImageDraw

    def make_icon_image():
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((6, 6, 58, 58), fill=(30, 144, 255, 255))
        d.rectangle((28, 18, 36, 40), fill=(255, 255, 255, 255))
        d.rectangle((28, 42, 36, 50), fill=(255, 255, 255, 255))
        return img

    class AgentApp:
        def __init__(self):
            self.base_url = os.environ.get("TIMETRACK_SERVER", "http://127.0.0.1:5000")
            self.ping_url = self.base_url.rstrip("/") + "/api/agent/ping"
            self.idle_seconds = _safe_env_int("TIMETRACK_IDLE", 60, minimum=1)
            self.ping_seconds = _safe_env_int("TIMETRACK_PING", 15, minimum=1)

            self.token = _agent_load_token()
            self.state = _AgentActivityState()

            self.running = threading.Event()
            self.running.set()

            self.stop_all = threading.Event()
            self.wake_up = threading.Event()

            self.icon = pystray.Icon(
                "TimeTrack",
                make_icon_image(),
                "TimeTrack",
                menu=pystray.Menu(
                    item("Статус", self.menu_status, enabled=False),
                    item("Пауза/Продолжить", self.toggle_running),
                    item("Выход", self.quit_app),
                ),
            )

            self._status_text = "Инициализация"

        def menu_status(self, icon, it):
            return

        def set_status(self, text: str):
            self._status_text = text
            try:
                self.icon.title = f"TimeTrack — {text}"
            except Exception:
                pass

        def toggle_running(self, icon, it):
            if self.running.is_set():
                self.running.clear()
                self.set_status("Пауза")
            else:
                self.state.bump()
                self.running.set()
                self.set_status("Работает")
            self.wake_up.set()

        def quit_app(self, icon, it):
            self.stop_all.set()
            self.wake_up.set()
            try:
                self.icon.stop()
            except Exception:
                pass

        def start_listeners(self):
            def on_mouse_move(x, y): self.state.bump()
            def on_click(x, y, button, pressed): self.state.bump()
            def on_scroll(x, y, dx, dy): self.state.bump()
            def on_key_press(key): self.state.bump()

            mouse.Listener(on_move=on_mouse_move, on_click=on_click, on_scroll=on_scroll).start()
            keyboard.Listener(on_press=on_key_press).start()

        def sync_ping(self) -> bool:
            if not self.running.is_set() and not self.token:
                self.set_status("Пауза")
                return False

            if not self.token:
                try:
                    self.set_status("Ожидание входа")
                    self.token = _agent_handshake(self.base_url, requests)
                except Exception as e:
                    _agent_log_line(f"handshake error: {e}")
                    self.set_status("Ошибка входа")
                    return False

            headers = {
                "Content-Type": "application/json",
                "X-Device-Token": self.token,
                "X-Device-Hash": _agent_device_hash(),
            }
            active = self.running.is_set() and self.state.active(self.idle_seconds)

            try:
                response = requests.post(self.ping_url, headers=headers, json={"active": bool(active)}, timeout=5)
                if response.status_code in (401, 403):
                    _agent_clear_token()
                    self.token = ""
                    self.set_status("Ð¢Ñ€ÐµÐ±ÑƒÐµÑ‚ÑÑ Ð²Ñ…Ð¾Ð´")
                    _agent_log_line(f"ping auth reset: {response.status_code}")
                    return False
                response.raise_for_status()
                if not self.running.is_set():
                    self.set_status("Пауза")
                else:
                    self.set_status("Активен" if active else "Неактивен")
                return True
            except Exception as e:
                _agent_log_line(f"ping error: {e}")
                self.set_status("Нет связи")
                return False

        def ping_loop(self):
            while not self.stop_all.is_set():
                ok = self.sync_ping()
                wait_seconds = self.ping_seconds if ok else 3
                self.wake_up.wait(wait_seconds)
                self.wake_up.clear()

        def run(self):
            self.start_listeners()
            t = threading.Thread(target=self.ping_loop, daemon=True)
            t.start()
            self.set_status("Работает")
            self.icon.run()

    try:
        AgentApp().run()
    except Exception as e:
        _agent_log_line(f"fatal: {e}")
        raise


def _run_server_forever():
    host = os.environ.get("TIMETRACK_HOST", "127.0.0.1")
    port = _safe_env_int("TIMETRACK_PORT", 5000, minimum=1)
    debug = os.environ.get("TIMETRACK_DEBUG", "0") == "1"
    app = create_app()
    app.run(host=host, port=port, debug=debug, use_reloader=False)


if __name__ == "__main__":
    mode = (os.environ.get("TIMETRACK_MODE") or "all").strip().lower()

    if mode == "agent":
        run_embedded_agent()
    elif mode == "server":
        _run_server_forever()
    else:
        host = os.environ.get("TIMETRACK_HOST", "127.0.0.1")
        port = _safe_env_int("TIMETRACK_PORT", 5000, minimum=1)
        t = threading.Thread(target=_run_server_forever, daemon=True)
        t.start()

        start_agent = os.environ.get("TIMETRACK_START_AGENT", "1") != "0"
        if start_agent:
            _wait_for_server(host, port)
            run_embedded_agent()
        else:
            t.join()
