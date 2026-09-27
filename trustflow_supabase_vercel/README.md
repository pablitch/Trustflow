# TrustFlow — migração da aplicação web para Supabase + Vercel

Pacote preparado em 27/09/2026. Contém a interface completa e a API, adaptadas das fontes disponíveis, SQL PostgreSQL e ferramentas para importar os dados. Não é um backup obtido diretamente da VM atual e não contém dados de produção nem senhas.

## O que está incluído

- Login com logo e Supabase Auth; perfis ADMIN, OPERADOR e SUPPLY.
- Dashboard, cadastros, início/finalização de fretes, doca, alarmes e reconhecimento.
- Histórico, gráficos, relatórios, laudo e modo demonstrativo com três fretes ativos.
- Ativos fixos, planta, associação/desassociação de sensores e histórico completo.
- API HTTPS dos sensores fixos, autenticação por chave e proteção contra leitura duplicada.
- Histórico sem corte em 200 leituras; campos de bateria, tensão e RSSI preservados na migração e exibidos na gestão de sensores.
- Exportação consistente do Azure SQL e importação com verificação de integridade, preservação de IDs e modo de teste.

A base é o backend integrado e a interface demonstrativa de 16/09/2026. O pacote de sinal/bateria de 01/09 complementou os campos de sensores. `referencia_azure/` contém código de referência, excluído da publicação na Vercel. O firmware ali é referência do pacote recebido, não uma nova versão pronta para gravar.

**Limite da entrega:** o `tcp_server.py` usado pela LilyGO não estava nos pacotes recebidos, inclusive no ZIP de sinal/bateria. A Vercel hospeda a aplicação HTTP; esta entrega não substitui o receptor TCP. A telemetria que continuar chegando ao Azure não aparecerá automaticamente no Supabase. A migração dos dispositivos e a virada de produção precisam ocorrer depois da homologação web. Não desligue a VM nesta etapa.

## 1. Preparar o Supabase

1. Use um projeto de homologação ou um banco sem o schema `trustflow`.
2. Abra o SQL Editor e execute o conteúdo de `sql/01_schema.sql`. Ele cria as tabelas; se o schema já existe, interrompe sem apagá-lo. Não execute sobre a instalação anterior sem verificar seu estado.
3. Em Authentication → Users, crie seu usuário com e-mail e senha. O usuário precisa estar confirmado para login por senha.
4. Copie o UUID desse usuário. Edite os valores do exemplo `sql/02_perfis.sql`, usando o UUID correto, seu nome e ADMIN, e execute. Não use o UUID fictício do exemplo.
5. Em Connect, copie a conexão do **Transaction pooler** (porta 6543). Use a senha do banco, não a senha do usuário do painel web. Se montar a URL manualmente, caracteres especiais da senha precisam de codificação de URL.
6. Copie a URL do projeto e sua chave publishable. Configure-as na Vercel conforme o passo 3.

O schema `trustflow` é privado, com RLS e sem acesso concedido a anon/authenticated. Não o adicione aos schemas expostos da Data API. A API Python valida a sessão no Supabase Auth e consulta o perfil no servidor. Inicialmente ela usa a conexão do proprietário do banco; a URL deve ficar somente nas variáveis secretas do servidor.

Usuários e senhas antigos não são convertidos automaticamente para Supabase Auth. Crie os acessos e associe seus perfis. Registros antigos exportados ficam preservados no arquivo privado de migração, sem servirem como credenciais do novo login.

## 2. Subir pelo Git

Extraia o ZIP. Abra o terminal **na pasta que contém `app.py`, `vercel.json`, `backend/` e `public/`**. Crie um repositório privado vazio no GitHub, sem README gerado automaticamente.

No Git Bash ou PowerShell, trocando SEU_USUARIO e SEU_REPOSITORIO:

```bash
git init
git add .
git commit -m "Migra TrustFlow web para Supabase e Vercel"
git branch -M main
git remote add origin https://github.com/SEU_USUARIO/SEU_REPOSITORIO.git
git push -u origin main
```

Se a pasta já tem Git, confira `git status` e `git remote -v` antes de adicionar o remoto. Se o `origin` já aponta para o repositório correto, pule o `git remote add`. Não use force push para resolver divergências.

O `.gitignore` exclui `.env`, dados exportados e dependências. Não mova senhas, chaves de dispositivos ou backups para arquivos rastreados. Para alterações futuras, use `git add .`, `git commit -m "Descrição da alteração"` e `git push`.

## 3. Publicar na Vercel

1. Importe esse repositório como um projeto na Vercel.
2. Selecione **FastAPI** como Framework Preset, com Root Directory na pasta que contém o `app.py` da raiz. Se o repositório tem os arquivos diretamente na raiz, mantenha `./`.
3. Deixe Build Command, Install Command e Output Directory no padrão, sem overrides. Remova as configurações antigas de site estático, como Framework `Other` e Output Directory `public`. Este pacote inclui um backend Python.
4. Configure as variáveis abaixo no ambiente em que vai publicar:

| Variável | Valor |
| --- | --- |
| `DATABASE_URL` | URL completa do Transaction pooler do Supabase |
| `SUPABASE_URL` | `https://SEU_PROJETO.supabase.co` |
| `SUPABASE_PUBLISHABLE_KEY` | Chave publishable do seu projeto |
| `DB_SSLMODE` | `require` |
| `COOKIE_SECURE` | `true` |
| `FIXED_STALE_SECONDS` | `2400` |
| `FIXED_DEVICE_KEYS_JSON` | `{}` inicialmente; depois, mapa das chaves dos sensores fixos |

`APP_ORIGIN` pode ficar ausente: a API usa a origem da própria requisição. Se um proxy alterar o host, configure a URL HTTPS exata do site, sem barra final. Um valor fixo de produção pode bloquear mutações em URLs de preview; configure por ambiente quando necessário.

5. Faça o deploy. Ao alterar variáveis depois, faça um novo deploy para aplicá-las.
6. Abra o site, confira a logo, o modo demonstrativo e entre com o usuário criado no Supabase.
7. `/api/health` verifica que a API responde. Depois de entrar, `/api/ready` verifica a consulta ao banco. Um health OK sozinho não comprova que o banco está configurado.

## 4. Trazer os dados reais do Azure

Leia `docs/MIGRACAO_DADOS.md` antes de executar. A tela iniciar vazia é normal antes da importação. Dados simulados ficam separados no navegador e não substituem a base real.

## 5. Rodar localmente (opcional)

Instale Python 3.12. Na pasta do projeto:

```bash
python -m venv .venv
```

No PowerShell: `.venv\Scripts\Activate.ps1`. No Git Bash/Linux: `source .venv/bin/activate` (no Git Bash do Windows, `source .venv/Scripts/activate`).

```bash
python -m pip install -r requirements.txt
```

Copie `.env.example` para `.env`, preencha as três variáveis do Supabase e use `COOKIE_SECURE=false` apenas para HTTP local. Inicie:

```bash
python -m uvicorn app:app --reload --port 8000
```

Abra http://localhost:8000. Não abra `index.html` diretamente no navegador: o login depende da API.

## Validação e limitações

Consulte `docs/VALIDACAO.md`. Foram executados testes locais, não um deploy real na sua conta. O exportador precisa ser validado contra o schema atual do Azure. A importação falha e reverte a transação se identificar incompatibilidade, destino preenchido ou integridade inválida; não corrige conflitos apagando histórico.

Históricos completos podem crescer bastante; não foi executado teste de carga com sua base real. A interface usa Chart.js e fontes externas, exigindo acesso à internet. Não foram integrados pagamentos reais: os estados de liquidação/smart contract mantêm a representação existente na aplicação.

Documentação de referência:
- https://vercel.com/docs/frameworks/backend/fastapi
- https://supabase.com/docs/guides/database/connecting-to-postgres
- https://supabase.com/docs/guides/auth
