-- Local equivalent of the Terraform-managed Cloud SQL databases/users.
CREATE USER governor WITH PASSWORD 'governor';
CREATE USER runtime WITH PASSWORD 'runtime';
CREATE DATABASE governor OWNER governor;
CREATE DATABASE runtime OWNER runtime;
\c runtime
CREATE EXTENSION IF NOT EXISTS vector;
