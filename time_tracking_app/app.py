from __future__ import annotations

import csv
import io
from datetime import datetime, date, time, timedelta

from flask import Flask, render_template, redirect, url_for, request, flash, send_file, jsonify, abort
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash

from models import db, User, Approval, AuditLog, DailyWorkStat, BreakRequest, Device


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "change-me"
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///time_tracking.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    db.init_app(app)

    login_manager = LoginManager()
    login_manager.login_view = "login"
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(User, int(user_id))

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

    def add_active_seconds(user_id: int, is_active: bool, now: datetime):
        """Увеличивает активное время пользователя на основании интервала между пингами.

        Время начисляется за интервал *между прошлым и текущим пингом*, если:
        - прошлое состояние было «активен»
        - пользователь не находится на утверждённом/ожидающем перерыве.
        """
        work_date = now.date()  # now передаётся как datetime.utcnow()
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

        # Первый пинг за день — просто фиксируем опорную точку.
        if stat.last_ping_at is None:
            stat.last_ping_at = now
            stat.last_state_active = is_active
            db.session.commit()
            return stat

        delta = (now - stat.last_ping_at).total_seconds()
        if delta < 0:
            delta = 0

        stale_limit = 30  # сек.
        if delta > stale_limit:
            # Большой разрыв — считаем данные устаревшими, интервал не прибавляем.
            stat.last_ping_at = now
            stat.last_state_active = is_active
            db.session.commit()
            return stat

        if stat.last_state_active and (not is_on_break(user_id, now)):
            stat.active_seconds += int(delta)

        stat.last_ping_at = now
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
        today = datetime.utcnow().date()
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
        for i in range(7):
            d = start_week + timedelta(days=i)
            s = stat_map.get(d)
            active_seconds = s.active_seconds if s else 0
            total_active_seconds += active_seconds
            days.append({"date": d, "active_seconds": active_seconds})

        now = datetime.utcnow()
        on_break = is_on_break(current_user.id, now)
        today_stat = DailyWorkStat.query.filter_by(user_id=current_user.id, work_date=today).first()
        is_active_now = False
        if today_stat and today_stat.last_ping_at:
            delta = (datetime.utcnow() - today_stat.last_ping_at).total_seconds()
            is_active_now = bool(today_stat.last_state_active) and (delta <= 30)

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
        stat = add_active_seconds(current_user.id, is_active, datetime.utcnow())
        h, m, s = format_hms(stat.active_seconds)
        return jsonify({"active_h": h, "active_m": m, "active_s": s, "is_active": is_active})

    @app.route("/api/me/live", methods=["GET"])
    @login_required
    def api_me_live():
        today = datetime.utcnow().date()
        stat = DailyWorkStat.query.filter_by(user_id=current_user.id, work_date=today).first()
        active_seconds = stat.active_seconds if stat else 0
        is_active_now = False
        if stat and stat.last_ping_at:
            delta = (datetime.utcnow() - stat.last_ping_at).total_seconds()
            is_active_now = bool(stat.last_state_active) and (delta <= 30)
        on_break = is_on_break(current_user.id, datetime.utcnow())
        total_seconds = active_seconds
        return jsonify({
            "date": today.isoformat(),
            "active_seconds": active_seconds,
            "total_seconds": total_seconds,
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
        add_active_seconds(dev.user_id, is_active, datetime.utcnow())

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
            dev_id = int(request.form.get("device_id", "0"))
            action = request.form.get("action", "")
            dev = Device.query.get_or_404(dev_id)
            if action == "disable":
                dev.enabled = False
                AuditLog.log(current_user.id, f"Отключено устройство {dev.id}")
            elif action == "enable":
                dev.enabled = True
                AuditLog.log(current_user.id, f"Включено устройство {dev.id}")
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
                minutes = int(request.form.get("minutes", "15"))
                minutes = max(5, min(minutes, 180))
                now = datetime.utcnow()
                br = BreakRequest(
                    user_id=current_user.id,
                    start_at=now,
                    end_at=now + timedelta(minutes=minutes),
                    status="PENDING",
                )
                db.session.add(br)
                db.session.commit()
                AuditLog.log(current_user.id, f"Перерыв {minutes} мин")
                flash("Заявка отправлена", "success")
                return redirect(url_for("breaks"))

            if current_user.role != "admin":
                flash("Нет доступа", "danger")
                return redirect(url_for("breaks"))

            br_id = int(request.form.get("break_id", "0"))
            br = BreakRequest.query.get_or_404(br_id)

            if action == "approve":
                br.status = "APPROVED"
                br.approved_by = current_user.id
                br.approved_at = datetime.utcnow()
                AuditLog.log(current_user.id, f"Утвержден перерыв {br.id}")
                flash("Утверждено", "success")
            elif action == "reject":
                br.status = "REJECTED"
                br.approved_by = current_user.id
                br.approved_at = datetime.utcnow()
                AuditLog.log(current_user.id, f"Отклонен перерыв {br.id}")
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

        today = datetime.utcnow().date()
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))
        start, end = period_bounds(year, month)

        if request.method == "POST":
            user_id = int(request.form["user_id"])
            action = request.form.get("action")

            appr = Approval.query.filter_by(user_id=user_id, year=year, month=month).first()
            if not appr:
                appr = Approval(user_id=user_id, year=year, month=month, status="DRAFT")
                db.session.add(appr)

            if action == "approve":
                appr.status = "APPROVED"
                appr.approved_by = current_user.id
                appr.approved_at = datetime.utcnow()
                AuditLog.log(current_user.id, f"Табель утвержден {user_id} {month:02d}.{year}")
                flash("Табель утвержден", "success")
            elif action == "reject":
                appr.status = "DRAFT"
                appr.approved_by = None
                appr.approved_at = None
                AuditLog.log(current_user.id, f"Табель сброшен {user_id} {month:02d}.{year}")
                flash("Статус сброшен", "info")

            db.session.commit()
            return redirect(url_for("approvals", year=year, month=month))

        if current_user.role == "admin":
            employees = User.query.filter(User.role == "employee").order_by(User.last_name).all()
        else:
            employees = (
                User.query.filter_by(department_id=current_user.department_id, role="employee")
                .order_by(User.last_name)
                .all()
            )

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
        today = datetime.utcnow().date()
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))
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

        user_id = int(user_id) if user_id else current_user.id
        allowed_ids = {u.id for u in selectable_users}
        if user_id not in allowed_ids:
            flash("Нет доступа", "danger")
            return redirect(url_for("reports"))

        selected_user = db.session.get(User, user_id)

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

        br = (
            BreakRequest.query.filter(
                BreakRequest.user_id == user_id,
                BreakRequest.start_at >= datetime.combine(start, time(0, 0)),
                BreakRequest.start_at < datetime.combine(end + timedelta(days=1), time(0, 0)),
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
        today = datetime.utcnow().date()
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))
        user_id = int(request.args.get("user_id", current_user.id))
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


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)
