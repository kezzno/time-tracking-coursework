from __future__ import annotations
from datetime import datetime
import secrets
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()

class Department(db.Model):
    __tablename__ = "departments"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)

class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    first_name = db.Column(db.String(100), nullable=False, default="Имя")
    last_name = db.Column(db.String(100), nullable=False, default="Фамилия")
    role = db.Column(db.String(20), nullable=False, default="employee")
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id"), nullable=True)
    department = db.relationship("Department", backref="users")

class Approval(db.Model):
    __tablename__ = "approvals"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    user = db.relationship("User", foreign_keys=[user_id], backref="approvals")
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="DRAFT")
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    approver = db.relationship("User", foreign_keys=[approved_by])
    approved_at = db.Column(db.DateTime, nullable=True)
    __table_args__ = (db.UniqueConstraint("user_id", "year", "month", name="uq_approval_period"),)

class AuditLog(db.Model):
    __tablename__ = "audit_log"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    action = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    @staticmethod
    def log(user_id: int | None, action: str, commit: bool = True):
        db.session.add(AuditLog(user_id=user_id, action=action))
        if commit:
            db.session.commit()

class DailyWorkStat(db.Model):
    __tablename__ = "daily_work_stats"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    user = db.relationship("User", backref="daily_stats")
    work_date = db.Column(db.Date, nullable=False)
    active_seconds = db.Column(db.Integer, nullable=False, default=0)
    last_ping_at = db.Column(db.DateTime, nullable=True)
    last_state_active = db.Column(db.Boolean, nullable=False, default=False)
    __table_args__ = (db.UniqueConstraint("user_id", "work_date", name="uq_daily_user_date"),)

class BreakRequest(db.Model):
    __tablename__ = "break_requests"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    user = db.relationship("User", foreign_keys=[user_id])
    requested_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    start_at = db.Column(db.DateTime, nullable=False)
    end_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="PENDING")
    approved_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    approver = db.relationship("User", foreign_keys=[approved_by])
    approved_at = db.Column(db.DateTime, nullable=True)
    comment = db.Column(db.String(500), nullable=True)

class Device(db.Model):
    __tablename__ = "devices"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    user = db.relationship("User", foreign_keys=[user_id])
    device_hash = db.Column(db.String(64), nullable=False)
    token = db.Column(db.String(64), nullable=False, unique=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    __table_args__ = (db.UniqueConstraint("user_id", "device_hash", name="uq_device_user_hash"),)

    @staticmethod
    def create(user_id: int, device_hash: str):
        return Device(user_id=user_id, device_hash=device_hash, token=secrets.token_hex(24), enabled=True)
