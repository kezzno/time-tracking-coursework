from app import create_app
from db_bootstrap import initialize_database


def main() -> None:
    app = create_app()
    with app.app_context():
        initialize_database(reset_existing_users=True)
        print(
            "OK: \u0411\u0430\u0437\u0430 \u0441\u043e\u0437\u0434\u0430\u043d\u0430. "
            "\u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u0438: "
            "admin/admin123, manager/manager123, employee/employee123"
        )


if __name__ == "__main__":
    main()
