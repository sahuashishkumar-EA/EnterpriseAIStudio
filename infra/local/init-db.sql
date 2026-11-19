-- Local equivalent of the Terraform-managed Cloud SQL databases/users.
CREATE USER governor WITH PASSWORD 'governor';
CREATE USER runtime WITH PASSWORD 'runtime';
CREATE DATABASE governor OWNER governor;
CREATE DATABASE runtime OWNER runtime;
\c runtime
CREATE EXTENSION IF NOT EXISTS vector;

-- Configuration registry (C04). The DDL in db/migrations creates schema `registry` and role
-- `registry_app` (SELECT/INSERT only); the service login is granted that role.
CREATE USER registry_owner WITH PASSWORD 'registry';
CREATE DATABASE registry OWNER registry_owner;
