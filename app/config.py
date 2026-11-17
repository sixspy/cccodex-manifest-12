import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Config:
    LAB_NAME = "湖区清单台"
    SECRET = os.environ.get("SECRET_KEY", "cc-manifest-dev-secret")
    ADMIN_USER = "admin"
    ADMIN_PASSWORD = "123456"
    DEFAULT_DB = str(ROOT / "instance" / "manifest_lab.db")
    DEFAULT_EXPORT_DIR = str(ROOT / "instance" / "exports")
    # 凭证二选一：本台只启用「物化不可变清单包」，不启用独立快照表
    EXPORT_CREDENTIAL = "manifest_package"

    @classmethod
    def database(cls) -> str:
        return os.environ.get("DATABASE_PATH", cls.DEFAULT_DB)

    @classmethod
    def export_dir(cls) -> str:
        return os.environ.get("EXPORT_DIR", cls.DEFAULT_EXPORT_DIR)
