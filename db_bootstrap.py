from __future__ import annotations

from werkzeug.security import generate_password_hash

from models import Department, User, db


DEFAULT_DEPARTMENTS = (
    "\u0418\u0422-\u043e\u0442\u0434\u0435\u043b",
    "HR",
)

DEFAULT_USERS = (
    {
        "username": "admin",
        "password": "admin123",
        "first_name": "\u0410\u0434\u043c\u0438\u043d",
        "last_name": "\u0421\u0438\u0441\u0442\u0435\u043c\u044b",
        "role": "admin",
        "department_name": "\u0418\u0422-\u043e\u0442\u0434\u0435\u043b",
    },
    {
        "username": "manager",
        "password": "manager123",
        "first_name": "\u0420\u0443\u043a\u043e\u0432\u043e\u0434\u0438\u0442\u0435\u043b\u044c",
        "last_name": "\u041e\u0442\u0434\u0435\u043b\u0430",
        "role": "manager",
        "department_name": "\u0418\u0422-\u043e\u0442\u0434\u0435\u043b",
    },
    {
        "username": "employee",
        "password": "employee123",
        "first_name": "\u0421\u043e\u0442\u0440\u0443\u0434\u043d\u0438\u043a",
        "last_name": "\u0422\u0435\u0441\u0442\u043e\u0432\u044b\u0439",
        "role": "employee",
        "department_name": "\u0418\u0422-\u043e\u0442\u0434\u0435\u043b",
    },
)


def ensure_department(name: str) -> Department:
    department = Department.query.filter_by(name=name).first()
    if department is None:
        department = Department(name=name)
        db.session.add(department)
        db.session.flush()
    return department


def ensure_user(
    *,
    username: str,
    password: str,
    first_name: str,
    last_name: str,
    role: str,
    department_id: int | None,
    reset_existing: bool,
) -> User:
    user = User.query.filter_by(username=username).first()
    password_hash = generate_password_hash(password)

    if user is None:
        user = User(
            username=username,
            password_hash=password_hash,
            first_name=first_name,
            last_name=last_name,
            role=role,
            department_id=department_id,
        )
        db.session.add(user)
        return user

    if reset_existing:
        user.password_hash = password_hash
        user.first_name = first_name
        user.last_name = last_name
        user.role = role
        user.department_id = department_id

    return user


def initialize_database(*, reset_existing_users: bool = False) -> None:
    db.create_all()

    departments: dict[str, Department] = {}
    for name in DEFAULT_DEPARTMENTS:
        departments[name] = ensure_department(name)

    for item in DEFAULT_USERS:
        department = departments.get(item["department_name"])
        ensure_user(
            username=item["username"],
            password=item["password"],
            first_name=item["first_name"],
            last_name=item["last_name"],
            role=item["role"],
            department_id=department.id if department else None,
            reset_existing=reset_existing_users,
        )

    db.session.commit()
