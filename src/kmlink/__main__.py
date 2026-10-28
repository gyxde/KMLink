if __name__ == "__main__":
    import sys
    from .updater import run_installer_cli
    result = run_installer_cli(sys.argv[1:])
    if result is not None:
        raise SystemExit(result)
    from .app import main
    main()
