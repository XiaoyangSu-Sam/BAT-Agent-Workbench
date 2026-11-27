import re

class HarnessError(Exception):
    def __init__(self, code, message, stage="runtime", retryable=False, **details):
        super().__init__(message)
        self.code, self.message = code, message
        self.stage, self.retryable, self.details = stage, retryable, details

    def as_dict(self):
        return dict(code=self.code, message=self.message, stage=self.stage,
                    retryable=self.retryable, **self.details)

def redact(value, secrets=()):
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[REDACTED]", text)


def clean(value, secrets):
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {k: clean(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v, secrets) for v in value]
    return value
