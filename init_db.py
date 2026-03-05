from werkzeug.security import generate_password_hash
from app import create_app
from models import db, Department, User


def ensure_department(name: str) -> Department:
    department = Department.query.filter_by(name=name).first()
    if department is None:
        department = Department(name=name)
        db.session.add(department)
        db.session.flush()
    return department


def ensure_user(
    username: str,
    password: str,
    first_name: str,
    last_name: str,
    role: str,
    department_id: int | None,
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

    user.password_hash = password_hash
    user.first_name = first_name
    user.last_name = last_name
    user.role = role
    user.department_id = department_id
    return user


def main():
    app = create_app()
    with app.app_context():
        db.create_all()

        it = ensure_department("ИТ-отдел")
        ensure_department("HR")

        ensure_user(
            username="admin",
            password="admin123",
            first_name="Админ",
            last_name="Системы",
            role="admin",
            department_id=it.id,
        )
        ensure_user(
            username="manager",
            password="manager123",
            first_name="Руководитель",
            last_name="Отдела",
            role="manager",
            department_id=it.id,
        )
        ensure_user(
            username="employee",
            password="employee123",
            first_name="Сотрудник",
            last_name="Тестовый",
            role="employee",
            department_id=it.id,
        )
        db.session.commit()

        print("OK: База создана. Пользователи: admin/admin123, manager/manager123, employee/employee123")

if __name__ == "__main__":
    main()
