"""Entry point used only by the standalone updater build."""
import multiprocessing

if __name__ == '__main__':
    multiprocessing.freeze_support()
    from dlss5tool.update_helper import main
    raise SystemExit(main())
