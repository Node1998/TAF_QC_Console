from app import app, init_db, upgrade_db

# Initialize and upgrade the database on application startup
init_db()
upgrade_db()

if __name__ == "__main__":
    app.run()
