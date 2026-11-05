-- Postgres only: no other database user (e.g. the other service) may connect to this
-- database. Each service connects with its own user to its own database.
DO $$
BEGIN
  EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', current_database());
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), current_user);
END $$;
