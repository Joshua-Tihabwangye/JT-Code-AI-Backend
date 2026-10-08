-- Store Auth's session lifetime in the location expected by Supabase services.
\getenv jwt_exp JWT_EXP
ALTER DATABASE postgres SET "app.settings.jwt_exp" TO :'jwt_exp';
