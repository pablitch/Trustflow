-- 1. Create the user in Supabase Authentication > Users (set password there).
-- 2. Copy the UUID and replace REPLACE_WITH_AUTH_USER_UUID below.
-- 3. Choose ADMIN / OPERADOR / SUPPLY explicitly. No automatic admins.
insert into trustflow.profiles(id, nome, perfil, ativo)
values ('REPLACE_WITH_AUTH_USER_UUID'::uuid, 'Seu nome', 'ADMIN', true)
on conflict (id) do update set nome=excluded.nome, perfil=excluded.perfil, ativo=excluded.ativo;
-- Existing public.trustflow_profiles from the earlier migration are NOT changed.
-- Explicitly provision the corresponding profiles in trustflow.profiles.
