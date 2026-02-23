from werkzeug.security import generate_password_hash
from app import create_app
from models import db, Department, User

def main():
    app = create_app()
    with app.app_context():
        db.drop_all()
        db.create_all()

        it = Department(name="ИТ-отдел")
        hr = Department(name="HR")
        db.session.add_all([it, hr])
        db.session.commit()

        admin = User(
            username="admin",
            password_hash=generate_password_hash("admin123"),
            first_name="Админ",
            last_name="Системы",
            role="admin",
            department_id=it.id,
        )
        manager = User(
            username="manager",
            password_hash=generate_password_hash("manager123"),
            first_name="Руководитель",
            last_name="Отдела",
            role="manager",
            department_id=it.id,
        )
        employee = User(
            username="employee",
            password_hash=generate_password_hash("employee123"),
            first_name="Сотрудник",
            last_name="Тестовый",
            role="employee",
            department_id=it.id,
        )
        db.session.add_all([admin, manager, employee])
        db.session.commit()

        print("OK: База создана. Пользователи: admin/admin123, manager/manager123, employee/employee123")

if __name__ == "__main__":
    main()
