# Dados reais e virada de produção

## Homologação primeiro

O exportador lê todas as tabelas do schema `dbo` do Azure SQL em uma transação SNAPSHOT. Não altera o banco. Se o modo SNAPSHOT não estiver disponível, interrompe; não muda configurações do Azure automaticamente. Os arquivos exportados contêm dados privados e podem conter os antigos hashes de senha. Mantenha-os fora do Git e não os envie para a Vercel.

Na VM, use o ambiente Python que já contém `mssql-python` e `python-dotenv`. Copie `scripts/exportar_azure.py` para uma pasta de trabalho e execute (ajuste o caminho do `.env` se necessário):

```bash
python scripts/exportar_azure.py --env-file /opt/trustflow/api/.env --output "$HOME/export_azure"
```

A pasta de saída precisa ser nova. O exportador lê `SQL_CONNECTION_STRING` do `.env`. A pasta terá `manifest.json` e arquivos JSONL com contagens e SHA-256. Um erro deixa uma exportação incompleta que não deve ser importada. Copie a pasta concluída para o computador que executará o importador, sem publicá-la.

No projeto novo, instale `requirements.txt`, configure `.env` para o Supabase de homologação e execute:

```bash
python scripts/importar_supabase.py /CAMINHO/DA/PASTA/export_azure
```

No Windows, substitua o caminho, por exemplo `C:/Users/SEU_USUARIO/Downloads/export_azure`.

O modo padrão valida hashes, contagens, tipos e vínculos, insere dentro de uma transação e a desfaz ao terminar. Se aparecer `DRY RUN OK`, repita com `--apply` para aplicar:

```bash
python scripts/importar_supabase.py /CAMINHO/DA/PASTA/export_azure --apply
```

O destino precisa estar vazio nas tabelas operacionais; perfis de login podem existir. Não cadastre sensores/fretes de teste no mesmo banco antes de importar. Use um projeto separado para esses testes. O importador não substitui registros existentes.

Os IDs conhecidos são mantidos e as sequências são ajustadas depois da importação. Se o identificador da origem tiver nome diferente do schema previsto, o processo para para ajustar o mapeamento, em vez de gerar outros IDs. Campos e tabelas `dbo` não modelados são preservados em `trustflow.azure_archive`, com acesso privado. Isso preserva o conteúdo, mas não cria telas ou funcionalidades para esses campos. O exportador não transfere procedures, jobs, triggers, permissões, arquivos da VM nem outros schemas.

Compare contagens do manifest, fretes ativos/finalizados, últimas leituras, alarmes e vínculos de sensores com a operação no Azure. Campos de data sem fuso são tratados como UTC, conforme o backend original; confirme essa convenção no banco atual. Crie as contas no Supabase Auth e libere os perfis separadamente.

## Corte definitivo

Não há sincronização contínua Azure → Supabase neste pacote. Uma exportação representa apenas o instante do snapshot. Enquanto a operação e os dispositivos escrevem no Azure, a cópia fica defasada.

Antes da virada, ainda é necessário obter o receptor `tcp_server.py` e os firmwares atuais da VM/dispositivos, adaptar ou manter um gateway de ingestão compatível e testar a telemetria. Não basta mudar a URL do frontend. A LilyGO usa TCP e não passa a falar HTTPS automaticamente. O firmware incluído em `referencia_azure/` não deve ser regravado nesta fase.

Depois de homologar o receptor e os dispositivos, programe uma janela de corte: interrompa novas alterações operacionais, drene/confira a fila de telemetria, obtenha o snapshot final e importe em um destino limpo. Mude a aplicação e o destino de ingestão juntos. Confirme frete ativo, histórico, duplicidade, alarmes, bateria/sinal e um ciclo offline antes de liberar a operação. Os dispositivos precisam preservar o buffer durante a janela. Não desligue o Azure antes de reconciliar as leituras recebidas após o snapshot.

Sensores fixos usam `/api/iot/fixos/{sensor_id}/config` e `/api/iot/fixos/{sensor_id}/leituras`, com `X-Device-Key`. Preserve suas chaves em `FIXED_DEVICE_KEYS_JSON`. URL HTTPS, confiança TLS e acesso do dispositivo ao domínio da Vercel precisam ser conferidos no firmware. Uma URL protegida por login de preview pode impedir o dispositivo de chamar a API.

Se houver necessidade de retorno, o Azure só será uma base segura para retomar após reconciliar os registros que tenham sido escritos no Supabase durante a virada. Não faça duas operações independentes gravarem as mesmas entidades sem um plano de reconciliação.
