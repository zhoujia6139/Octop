# FnOS legacy packages retired

The native and full-server FPK launchers are retired, and the release pipeline no longer builds or advertises them. Their source remains historical reference only. On a NAS deploy the current [cloud-only Docker service](../deploy/docker/README.md), expose an HTTPS origin and use Octop Desktop for Agent execution. There is no cloud execution fallback or FnOS setup wizard.
