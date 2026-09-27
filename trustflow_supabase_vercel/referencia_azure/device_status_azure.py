from typing import Any


def _numero(payload: dict[str, Any], campo: str) -> float | None:
    valor = payload.get(campo)
    if valor is None:
        return None
    try:
        return float(valor)
    except (TypeError, ValueError):
        return None


def atualizar_status_dispositivo(cursor: Any, payload: dict[str, Any]) -> None:
    """Atualiza somente o status operacional enviado por uma telemetria valida.

    A transacao e o commit continuam sob controle do tcp_server.py.
    """
    sensor_id = str(payload.get('sensor_id') or '').strip()
    if not sensor_id:
        return

    rssi = _numero(payload, 'rssi')
    bateria = _numero(payload, 'bateria')
    bateria_volts = _numero(payload, 'bateria_volts')

    if rssi is not None and not -120 <= rssi <= -40:
        rssi = None
    if bateria is not None and not 0 <= bateria <= 100:
        bateria = None
    if bateria_volts is not None and not 2.5 <= bateria_volts <= 5.0:
        bateria_volts = None

    cursor.execute(
        '''
        UPDATE dbo.Dispositivos
        SET
            SinalRssi = COALESCE(?, SinalRssi),
            Bateria = COALESCE(?, Bateria),
            BateriaVolts = COALESCE(?, BateriaVolts),
            UltimaComunicacao = SYSUTCDATETIME()
        WHERE SensorId = ?;
        ''',
        (
            int(rssi) if rssi is not None else None,
            int(round(bateria)) if bateria is not None else None,
            round(bateria_volts, 3) if bateria_volts is not None else None,
            sensor_id
        )
    )
