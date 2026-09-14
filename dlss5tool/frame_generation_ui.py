"""Compatibility entry: frame generation now lives in the main export panel."""


def main():
    import multiprocessing
    multiprocessing.freeze_support()
    from dlss5tool.gui import cli
    cli()


if __name__ == '__main__':
    main()
