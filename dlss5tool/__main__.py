"""Launch with python -m dlss5tool."""
import multiprocessing


if __name__ == '__main__':
    multiprocessing.freeze_support()
    from dlss5tool.adapter_probe import dispatch
    if dispatch():
        raise SystemExit(0)
    from dlss5tool.resident_sr_worker import dispatch as dispatch_resident_sr
    if dispatch_resident_sr():
        raise SystemExit(0)
    from dlss5tool.gui import cli
    cli()
