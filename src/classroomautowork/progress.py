"""Measured progress; a string callback remains compatible with CLI callers."""


class ProgressUpdate(str):
    def __new__(cls, message, stage, **details):
        value = super().__new__(cls, message)
        value.details = {"stage": stage, **details}
        return value


def report(callback, message, stage, **details):
    if callback:
        callback(ProgressUpdate(message, stage, **details))
