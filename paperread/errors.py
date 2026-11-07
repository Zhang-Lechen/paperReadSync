class AppError(RuntimeError):
    """Public error code and fixed message, never raw transport/input content."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")
