"""Error data safe to retain across worker/UI lifetimes without holding frames."""
import traceback


class ErrorSnapshot:
    """Keep text/type and scalar pressure data; rebuild exceptions per consumer.

    Keeping even a traceback-cleared exception is insufficient: raising that
    same object again would attach the consumer's frames to the stored object.
    Original args, cause/context and custom attributes can also own arrays.
    """
    def __init__(self, error):
        self.error_type = type(error)
        self.message = str(error)
        self.traceback = ''.join(traceback.format_exception(error))
        original = getattr(error, 'diagnostic_traceback', None)
        if original:
            self.traceback += '\nOriginal worker traceback:\n' + original
        from dlss5tool.memory_budget import pressure_details
        self.memory_pressure = pressure_details(error)

    def __str__(self):
        return self.message

    def exception(self):
        try:
            error = self.error_type(self.message)
            if str(error) != self.message:
                error = RuntimeError(self.message)
        except Exception:
            # Some exception types require structured constructor arguments.
            error = RuntimeError(self.message)
        error.diagnostic_traceback = self.traceback
        if self.memory_pressure is not None:
            from dlss5tool.memory_budget import copy_pressure_details
            error.memory_pressure = copy_pressure_details(self.memory_pressure)
        return error
