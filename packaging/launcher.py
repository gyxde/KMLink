"""PyInstaller entry point; the installed source entry point is python -m kmlink."""

if __name__ == "__main__":
    import sys
    from kmlink.updater import run_installer_cli
    result = run_installer_cli(sys.argv[1:])
    if result is not None:
        raise SystemExit(result)
    from kmlink.app import main
    main()
