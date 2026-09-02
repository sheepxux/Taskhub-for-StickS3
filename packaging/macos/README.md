# macOS Host package

Build an unsigned development installer:

```bash
./packaging/macos/build_host_pkg.sh
```

The package is written to `dist/macos/`. It installs the Host code from an
explicit allowlist, creates a random per-user device token when needed,
registers the LaunchAgent, adds `/usr/local/bin/taskhub-pair` and
`/usr/local/bin/taskhub-provision`, and installs `/Applications/TaskHub Host.app`.
The app opens the StickS3 pairing dialog (or diagnostics); `postinstall`
launches it once so the installer ends on "type the code shown on your Stick".

Public distribution requires a Developer ID Installer certificate:

```bash
TASKHUB_INSTALLER_IDENTITY="Developer ID Installer: Example (TEAMID)" \
TASKHUB_NOTARY_PROFILE="taskhub-notary" \
./packaging/macos/build_host_pkg.sh
```

`TASKHUB_NOTARY_PROFILE` is an optional `notarytool` keychain profile. The build
script never copies firmware secrets, Host tokens, models, logs, caches, or
repository metadata into the package.
