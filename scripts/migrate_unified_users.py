"""Create unified user tables using the service's existing DB configuration."""
from parse_video_py.user_db import init_user_database


if __name__ == "__main__":
    init_user_database()
    print("Unified user tables are ready.")
