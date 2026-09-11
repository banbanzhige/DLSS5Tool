"""Compatibility launcher: python gui.py or double-click run.bat."""
import multiprocessing
import sys


if __name__ == '__main__':
    # Worker dispatch must happen before the portable GUI installation mutex.
    multiprocessing.freeze_support()
    if getattr(sys, 'frozen', False) and not any(
            flag in sys.argv for flag in ('--diagnostic-worker', '--parallel-worker', '--selftest', '--vsr-selftest')):
        from pathlib import Path
        from dlss5tool import delta_update, update_helper
        root = Path(sys.executable).parent
        try:
            with update_helper.installation_lock(root):
                state = delta_update.status(root) or {}
                if state.get('phase') in ('waiting', 'applying', 'recovery_needed'):
                    raise delta_update.DeltaError(
                        '更新尚未结束，请等待助手；若助手已退出，请关闭程序后运行 / Update pending; '
                        'wait for the helper, or recover after it exits:\n'
                        f'"{root / delta_update.TRANSACTION / delta_update.HELPER}" --root "{root}" --recover')
                from dlss5tool.gui import cli
                cli()
        except delta_update.DeltaError as error:
            update_helper.notify(str(error), error=True)
    else:
        from dlss5tool.gui import cli
        cli()
