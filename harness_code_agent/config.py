"""
Harness configuration.
Uses OpenAI-compatible API so it works with any provider.

Setup:
  cp .env.template .env   # then fill in your real values
"""
import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv():
    """Load .env file if it exists. No third-party dependency needed."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        # .env takes priority over shell env vars
        if key:
            os.environ[key] = value


_load_dotenv()

# --- API ---
API_KEY = os.environ.get("OPENAI_API_KEY", "")
BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
BASE_MODEL = os.environ.get("HARNESS_MODEL", "gpt-4o")
PROVIDER = os.environ.get("HARNESS_PROVIDER", "auto")
STREAM = os.environ.get("HARNESS_STREAM", "auto")

MODEL_INTENSITIES = ("fast", "normal", "hard", "max")
MODEL_OVERRIDES = {
    intensity: os.environ.get(f"HARNESS_MODEL_{intensity.upper()}")
    for intensity in MODEL_INTENSITIES
}


@dataclass(frozen=True)
class ModelProfile:
    provider: str
    model: str
    thinking: bool | None = None
    reasoning_effort: str | None = None


def _normalize_model_intensity(value: str | None) -> str:
    intensity = (value or "hard").strip().lower()
    if intensity not in MODEL_INTENSITIES:
        allowed = ", ".join(MODEL_INTENSITIES)
        raise ValueError(f"Unsupported HARNESS_MODEL_INTENSITY: {value!r}; expected one of: {allowed}")
    return intensity


def _resolve_provider_name(*, provider: str | None, base_url: str | None, model: str | None) -> str:
    requested = (provider or "auto").strip().lower()
    if requested in {"openai-compatible", "compatible"}:
        return "openai-compatible"
    if requested in {"openai", "deepseek"}:
        return requested
    if requested != "auto":
        raise ValueError(f"Unsupported HARNESS_PROVIDER: {provider}")

    signature = f"{base_url or ''} {model or ''}".lower()
    if "deepseek" in signature:
        return "deepseek"
    if "api.openai.com" in (base_url or "").lower():
        return "openai"
    return "openai-compatible"


def _model_override_for_intensity(intensity: str) -> str | None:
    return MODEL_OVERRIDES.get(intensity)


def _default_model_profile(provider: str, intensity: str) -> ModelProfile:
    if provider == "deepseek":
        defaults = {
            "fast": ModelProfile(provider=provider, model="deepseek-v4-flash", thinking=False),
            "normal": ModelProfile(
                provider=provider,
                model="deepseek-v4-flash",
                thinking=True,
                reasoning_effort="high",
            ),
            "hard": ModelProfile(
                provider=provider,
                model="deepseek-v4-pro",
                thinking=True,
                reasoning_effort="high",
            ),
            "max": ModelProfile(
                provider=provider,
                model="deepseek-v4-pro",
                thinking=True,
                reasoning_effort="max",
            ),
        }
        return defaults[intensity]
    return ModelProfile(provider=provider, model=BASE_MODEL)


def resolve_model_profile(intensity: str) -> ModelProfile:
    normalized = _normalize_model_intensity(intensity)
    override_model = _model_override_for_intensity(normalized)
    provider = _resolve_provider_name(
        provider=PROVIDER,
        base_url=BASE_URL,
        model=override_model or BASE_MODEL,
    )
    profile = _default_model_profile(provider, normalized)
    if override_model:
        profile = ModelProfile(
            provider=profile.provider,
            model=override_model,
            thinking=profile.thinking,
            reasoning_effort=profile.reasoning_effort,
        )
    return profile


MODEL_INTENSITY = _normalize_model_intensity(os.environ.get("HARNESS_MODEL_INTENSITY", "hard"))
MODEL_PROFILES = {intensity: resolve_model_profile(intensity) for intensity in MODEL_INTENSITIES}
MODEL_PROFILE = MODEL_PROFILES[MODEL_INTENSITY]
MODEL = MODEL_PROFILE.model

# --- Token budgets ---
CONTEXT_WINDOW_TOKENS = int(os.environ.get("HARNESS_CONTEXT_WINDOW_TOKENS", "200000"))
COMPRESS_THRESHOLD = int(os.environ.get("COMPRESS_THRESHOLD", str(int(CONTEXT_WINDOW_TOKENS * 0.90))))
SUMMARY_TARGET_THRESHOLD = int(os.environ.get("SUMMARY_TARGET_THRESHOLD", str(int(CONTEXT_WINDOW_TOKENS * 0.75))))

# --- Harness loop ---
MAX_HARNESS_ROUNDS = int(os.environ.get("MAX_HARNESS_ROUNDS", "5"))
PASS_THRESHOLD = float(os.environ.get("PASS_THRESHOLD", "7.0"))

# --- Agent limits ---
MAX_AGENT_ITERATIONS = int(os.environ.get("MAX_AGENT_ITERATIONS", "60"))
MAX_AGENT_TOTAL_TOKENS = int(os.environ.get("MAX_AGENT_TOTAL_TOKENS", "300000"))
MAX_AGENT_TOOL_CALLS = int(os.environ.get("MAX_AGENT_TOOL_CALLS", "200"))
AGENT_BUDGET_WARN_FRACTION = float(os.environ.get("AGENT_BUDGET_WARN_FRACTION", "0.8"))
MAX_TOOL_ERRORS = 5           # consecutive tool errors before abort
TRACE_STDERR = os.environ.get("HARNESS_TRACE_STDERR", "").lower() in {"1", "true", "yes", "on"}
WINDOWS_SHELL = os.environ.get("HARNESS_WINDOWS_SHELL", "auto")
SANDBOX_MODE = os.environ.get("HARNESS_SANDBOX_MODE", "host")
DOCKER_IMAGE = os.environ.get("HARNESS_DOCKER_IMAGE", "python:3.12")
DOCKER_NETWORK = os.environ.get("HARNESS_DOCKER_NETWORK", "none")
DOCKER_USER = os.environ.get("HARNESS_DOCKER_USER", "")

# --- Paths ---
WORKSPACE = os.path.abspath(os.environ.get("HARNESS_WORKSPACE", "./workspace"))
SPEC_FILE = "spec.md"
FEEDBACK_FILE = "feedback.md"
CONTRACT_FILE = "contract.md"
PROGRESS_FILE = "progress.md"
