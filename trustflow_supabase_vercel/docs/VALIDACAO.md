# Validação realizada

27/09/2026 — teste local com Python, FastAPI TestClient e PostgreSQL embarcado PGlite. O serviço externo Supabase Auth foi simulado; validação de perfil e SQL foram executadas no banco de teste. Isso não valida rede, pooler hospedado, credenciais reais ou concorrência em produção.

11 testes aprovados:
- Interface/arquivo da logo, health e bloqueio de acesso sem login.
- Perfil ativo/inativo, readiness e logout.
- Cookies HttpOnly, refresh sem expor token no JSON e rejeição de origem cruzada.
- Cadastros e atualização de sensor existente.
- Frete, leitura manual, alarme, reconhecimento repetido, doca e finalização; finalizar novamente um frete antigo não libera o sensor de outro frete.
- Ativos fixos, chave do dispositivo, leitura duplicada, conflito de conteúdo, posição na planta e desvinculação preservando 351 leituras.
- Impedimento de usar em frete um sensor vinculado a ativo fixo.
- Restrição por perfil de leitura manual e reintegração.
- Histórico de frete com 350 leituras.
- Importação sintética em modo de teste e aplicação, IDs, sequências e recusa de destino preenchido.
- Rejeição de arquivo exportado adulterado.

A interface foi exercitada em DOM simulado com jsdom: inicialização, modo demonstrativo, três cenários ativos, histórico, navegação e reinício. Chart.js/canvas foram simulados; não equivale a inspeção visual em navegador real. O SQL também passou por análise sintática local.

Não realizados: exportação no Azure real, comparação com o schema atual da VM, importação da base real, Supabase Auth real, deploy Vercel real, inspeção visual real, carga/concorrência e comunicação com sensores físicos. Esses pontos permanecem na homologação.

## Repetir os testes locais

Requer Python 3.12 e Node.js. Na raiz do pacote:

```bash
python -m pip install -r requirements.txt pytest==8.3.5
npm --prefix tests/embedded install
```

Git Bash/Linux:

```bash
TRUSTFLOW_EMBEDDED_TEST=1 python -m pytest -q tests
```

PowerShell:

```powershell
$env:TRUSTFLOW_EMBEDDED_TEST="1"
python -m pytest -q tests
```

O banco embarcado é descartável e não usa suas credenciais. Não configure TEST_DATABASE_URL para um banco operacional: o modo de banco externo da suíte limpa tabelas para preparar os cenários.
