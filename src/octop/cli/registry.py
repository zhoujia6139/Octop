"""Only supported product commands are discoverable or executable."""

COMMANDS: dict[str, tuple[str, str, str]] = {
    "run": (".commands.run", "run", "Run cloud management or the local sidecar."),
    "cloud-init": (".commands.application", "cloud_init", "Create the first cloud administrator."),
    "local-config": (
        ".commands.application",
        "local_config",
        "Configure the desktop cloud origin.",
    ),
    "health": (".commands.application", "health", "Check an application's health and role."),
    "version": (".commands.version", "version", "Show the installed version."),
    "completion": (".commands.completion", "completion", "Shell completion utilities."),
}
