from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(PROJECT_ROOT / '.env'), extra='ignore')
    app_name: str = 'Nebula Notebook'
    data_dir: Path = Path('./data')
    database_url: str = ''
    admin_email: str = ''
    admin_password: str = ''
    allowed_origins: str = 'http://localhost:5173,http://127.0.0.1:5173,http://127.0.0.1:8000,http://localhost:8000'
    secure_cookie: bool = False
    session_hours: int = 12
    max_cpu_sessions_per_user: int = 2
    max_gpu_sessions_per_user: int = 1
    cpu_image: str = 'nebula-runtime:cpu'
    gpu_image: str = 'nebula-runtime:gpu'
    runtime_memory: str = '2g'
    runtime_cpus: float = 2.0
    max_notebook_bytes: int = 4_000_000
    max_cell_source_chars: int = 150_000
    kernel_start_timeout: int = 50
    gpu_probe_timeout: int = 25

    @property
    def origins(self) -> set[str]:
        return {s.strip().rstrip('/') for s in self.allowed_origins.split(',') if s.strip()}


settings = Settings()
if not settings.data_dir.is_absolute():
    settings.data_dir = (PROJECT_ROOT / settings.data_dir).resolve()
settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
settings.data_dir.chmod(0o700)
