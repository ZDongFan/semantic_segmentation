"""从部署环境读取配置，不提供默认口令。"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    database_url: str
    storage_root: Path
    public_key: str
    max_upload_bytes: int = 1024 * 1024 * 1024

    def validate(self):
        """拒绝缺失的外部凭据及不受控目录。"""
        if not self.database_url:
            raise ValueError("必须配置数据库。")
        if len(self.public_key) < 32:
            raise ValueError("外部 API Key 须至少 32 字符。")
        if not self.storage_root.is_absolute() or self.max_upload_bytes <= 0:
            raise ValueError("存储目录必须为绝对路径，上传限制必须为正数。")

    @classmethod
    def from_env(cls):
        """读取部署文件，已有进程环境变量优先。"""
        load_dotenv(Path(os.environ.get("LCC_ENV_FILE", Path(__file__).resolve().parent.parent / ".env")),
                    override=False)
        return cls(
            os.environ["LCC_DATABASE_URL"],
            Path(os.environ["LCC_STORAGE_ROOT"]),
            os.environ["LCC_PUBLIC_API_KEY"],
            int(os.environ.get("LCC_MAX_UPLOAD_BYTES", 1024 * 1024 * 1024)),
        )
