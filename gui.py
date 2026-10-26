"""Compatibility launcher: python gui.py or double-click run.bat."""
import multiprocessing
import sys


if __name__ == '__main__':
    # Worker dispatch must happen before the portable GUI installation mutex.
    multiprocessing.freeze_support()
    from dlss5tool.adapter_probe import dispatch as dispatch_adapter_probe
    if dispatch_adapter_probe():
        raise SystemExit(0)
    from dlss5tool.resident_sr_worker import dispatch as dispatch_resident_sr
    if dispatch_resident_sr():
        raise SystemExit(0)
    if '--nvofa-selftest' in sys.argv:
        from dlss5tool.nvofa_selftest import main
        main(sys.argv[sys.argv.index('--nvofa-selftest') + 1:])
        raise SystemExit(0)
    if '--nvofa-worker' in sys.argv:
        sys.argv.remove('--nvofa-worker')
        from dlss5tool.guidance_worker import main
        main()
        raise SystemExit(0)
    if getattr(sys, 'frozen', False) and not any(
            flag in sys.argv for flag in ('--diagnostic-worker', '--parallel-worker', '--selftest', '--vsr-selftest', '--frame-generation-check')):
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
