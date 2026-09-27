# Integração do status de sinal e bateria no receptor TCP

O serviço `trustflow-tcp.service` aponta para `/opt/trustflow/api/tcp_server.py`, mas esse arquivo não estava no pacote recebido. Para preservar o receptor que já funciona, aplique somente os dois pontos abaixo no arquivo atualmente usado pela VM.

## 1. Importe o helper

Perto dos demais imports de `tcp_server.py`, adicione:

```python
from device_status import atualizar_status_dispositivo
```

## 2. Atualize o dispositivo na mesma transação da telemetria

No bloco que trata `evento == 'telemetria'`, depois de validar `device_key` e `sensor_id` e usando o mesmo `cursor` da gravação da telemetria, adicione:

```python
atualizar_status_dispositivo(cursor, dados)
```

Use o dicionário que veio do `json.loads(...)`. Se ele tiver outro nome, substitua apenas `dados` por esse nome. Não crie um segundo `commit`: o helper participa da transação que o receptor já usa.

Exemplo de posição:

```python
if evento == 'telemetria':
    # ... validações e INSERT já existentes permanecem iguais ...

    atualizar_status_dispositivo(cursor, dados)

    # ... resposta TELEMETRIA_CONFIRMADA e commit já existentes ...
```

Os campos novos são opcionais. Firmware antigo continua funcionando, porque valores ausentes não apagam o último RSSI ou a última bateria válida.

## Ordem segura de publicação

1. Execute `sql/trustflow_status_sinal_bateria.sql`.
2. Copie `backend/device_status.py` para `/opt/trustflow/api/device_status.py`.
3. Aplique as duas linhas acima no `tcp_server.py` atual.
4. Publique `backend/app.py` e `frontend/index.html`.
5. Reinicie `trustflow-tcp` e `trustflow-api`.
6. Grave o firmware novo e aguarde uma telemetria online.

## Validação rápida

No log serial, o JSON online deve conter campos semelhantes a:

```json
{"rssi":-81,"bateria":87,"bateria_volts":4.075}
```

No banco, confirme:

```sql
SELECT SensorId, SinalRssi, Bateria, BateriaVolts, UltimaComunicacao
FROM dbo.Dispositivos
WHERE SensorId = 'TF-ENV-001';
```

Leituras do buffer offline enviam esses campos como `null`, portanto não sobrescrevem o último status online válido.
