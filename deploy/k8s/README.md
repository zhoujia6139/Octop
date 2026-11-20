# Kubernetes deployment

Use the current [cloud Helm chart](../helm/octop/README.md). It replaces the old combined Agent-server cluster arrangement. Cloud runs one gateway with PostgreSQL; tools and workspaces run in Octop Desktop. There are no IM/cron leases, required vector extension or shared Agent workspaces in this deployment.
