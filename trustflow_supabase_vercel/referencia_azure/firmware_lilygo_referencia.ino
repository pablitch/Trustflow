/*
TRUSTFLOW - A7670E + AHT10 + TCP LONG POLLING + BUFFER OFFLINE v2

Com 4G:
- confirma o frete e envia temperatura/umidade a cada 1 minuto;
- reenvia um registro pendente do buffer a cada minuto.

Sem 4G:
- se já havia um frete ativo conhecido iniciado online, mede e grava na LittleFS a cada 5 minutos;
- os dados permanecem após desligar/reiniciar a LilyGO.

IMPORTANTE:
- selecione uma Partition Scheme da ESP32 que tenha SPIFFS/LittleFS;
- configure DEVICE_KEY abaixo com a mesma DEVICE_API_KEY da VM;
- o backend atual precisa manter o frete ATIVO para aceitar telemetrias reenviadas;\n- se o frete terminar durante a queda, as leituras ficam em arquivo órfão e não entram em outro frete.
*/

#define TINY_GSM_MODEM_A7670

#include <Wire.h>
#include <Adafruit_AHTX0.h>
#include <TinyGsmClient.h>
#include <FS.h>
#include <LittleFS.h>

// =====================================================
// IDENTIDADE DO DISPOSITIVO
// Deve corresponder exatamente ao cadastro no Azure SQL.
// =====================================================
const char SENSOR_ID[] = "TF-ENV-001";

// =====================================================
// SERVIDOR TCP NA VM AZURE
// =====================================================
const char TCP_SERVER[] = "20.83.57.133";
const uint16_t TCP_PORT = 9000;

// Deve ser exatamente igual ao DEVICE_API_KEY do .env da VM.
// Não publique essa chave em GitHub ou prints.
const char DEVICE_KEY[] =
  "COLOQUE_AQUI_A_MESMA_DEVICE_API_KEY_DA_VM";

// =====================================================
// DADOS DA VIVO
// =====================================================
const char APN[] = "zap.vivo.com.br";
const char USER[] = "vivo";
const char PASS[] = "vivo";

// =====================================================
// AHT10
// =====================================================
#define I2C_SDA 21
#define I2C_SCL 22

// =====================================================
// PINOS DO MODEM A7670E
// =====================================================
#define MODEM_TX_PIN 26
#define MODEM_RX_PIN 25
#define MODEM_PWRKEY_PIN 4
#define MODEM_RESET_PIN 27
#define MODEM_DTR_PIN 14
#define MODEM_POWER_ON_PIN 12

// =====================================================
// TEMPOS
// =====================================================
// Com 4G: leitura e envio normal a cada 1 minuto.
const uint32_t INTERVALO_ENVIO_ONLINE_MS = 60UL * 1000UL;

// Sem 4G, mas com frete ativo conhecido: salva na flash a cada 5 minutos.
const uint32_t INTERVALO_BUFFER_OFFLINE_MS = 5UL * 60UL * 1000UL;

// Evita reinicializar o modem continuamente durante uma queda longa.
const uint32_t INTERVALO_TENTATIVA_REDE_MS = 60UL * 1000UL;

// A cada ciclo online de 1 minuto, reenvia no máximo um registro antigo.
// Como o buffer cresce a cada 5 minutos, ele é drenado mais rápido do que cresce.
const uint8_t MAX_REENVIO_BUFFER_POR_CICLO = 1;

// Limite de proteção para não lotar a flash.
const size_t LIMITE_BUFFER_BYTES = 512UL * 1024UL;

// Arquivos internos LittleFS.
const char ARQUIVO_BUFFER[] = "/buffer_offline.ndjson";
const char ARQUIVO_BUFFER_TEMP[] = "/buffer_offline.tmp";
const char ARQUIVO_BUFFER_ORFAO[] = "/buffer_offline_orfao.ndjson";
const char ARQUIVO_ESTADO_FRETE[] = "/frete_ativo.txt";

// Tempo que a LilyGO espera a VM responder se já existe frete.
// O servidor segura a conexão por até 60s.
const uint32_t TIMEOUT_AGUARDAR_FRETE_MS = 75000;

const uint32_t TIMEOUT_AT_CURTO_MS = 5000;
const uint32_t TIMEOUT_AT_MEDIO_MS = 15000;
const uint32_t TIMEOUT_SOCKET_MS = 120000;
const uint32_t PAUSA_SEM_FRETE_MS = 2000;

// =====================================================
// OBJETOS
// =====================================================
HardwareSerial SerialAT(1);
TinyGsm modem(SerialAT);
Adafruit_AHTX0 aht;

// =====================================================
// ESTADO
// =====================================================
unsigned long ultimoEnvioOnline = 0;
unsigned long ultimaGravacaoOffline = 0;
unsigned long ultimaTentativaRede = 0;

bool modemConectado = false;
bool freteAtivo = false;
bool littleFsOk = false;
bool rede4GAnterior = false;

// =====================================================
// AUXILIARES
// =====================================================
void limparBufferModem() {
  while (SerialAT.available()) {
    SerialAT.read();
  }
}

void iniciarPinosModem() {
  pinMode(MODEM_POWER_ON_PIN, OUTPUT);
  digitalWrite(MODEM_POWER_ON_PIN, HIGH);

  pinMode(MODEM_PWRKEY_PIN, OUTPUT);
  digitalWrite(MODEM_PWRKEY_PIN, HIGH);

  pinMode(MODEM_RESET_PIN, OUTPUT);
  digitalWrite(MODEM_RESET_PIN, HIGH);

  pinMode(MODEM_DTR_PIN, OUTPUT);
  digitalWrite(MODEM_DTR_PIN, LOW);
}

bool esperarResposta(
  const String& textoEsperado,
  uint32_t timeoutMs,
  String& resposta
) {
  unsigned long inicio = millis();

  resposta = "";

  while (millis() - inicio < timeoutMs) {
    while (SerialAT.available()) {
      char caractere = SerialAT.read();

      resposta += caractere;
      Serial.write(caractere);

      if (
        resposta.indexOf("\r\nERROR\r\n") >= 0 ||
        resposta.indexOf("+CME ERROR:") >= 0 ||
        resposta.indexOf("+IP ERROR:") >= 0 ||
        resposta.indexOf("+CIPERROR:") >= 0
      ) {
        return false;
      }

      int posTexto = resposta.indexOf(textoEsperado);

      if (posTexto >= 0) {
        int fimLinha = resposta.indexOf("\r\n", posTexto);

        if (fimLinha >= 0) {
          return true;
        }
      }
    }
  }

  return false;
}

bool enviarComandoAT(
  const String& comando,
  const String& textoEsperado,
  uint32_t timeoutMs,
  String& resposta
) {
  limparBufferModem();

  Serial.print("\n>> ");
  Serial.println(comando);

  SerialAT.println(comando);

  return esperarResposta(
    textoEsperado,
    timeoutMs,
    resposta
  );
}

bool enviarComandoAT(
  const String& comando,
  const String& textoEsperado,
  uint32_t timeoutMs
) {
  String resposta;

  return enviarComandoAT(
    comando,
    textoEsperado,
    timeoutMs,
    resposta
  );
}

bool aguardarPromptTCP(
  uint32_t timeoutMs,
  String& resposta
) {
  unsigned long inicio = millis();

  resposta = "";

  while (millis() - inicio < timeoutMs) {
    while (SerialAT.available()) {
      char caractere = SerialAT.read();

      resposta += caractere;
      Serial.write(caractere);

      if (
        resposta.indexOf("\r\nERROR\r\n") >= 0 ||
        resposta.indexOf("+CME ERROR:") >= 0 ||
        resposta.indexOf("+IP ERROR:") >= 0 ||
        resposta.indexOf("+CIPERROR:") >= 0
      ) {
        return false;
      }

      if (resposta.indexOf(">") >= 0) {
        return true;
      }
    }
  }

  return false;
}

bool aguardarRespostaServidor(
  uint32_t timeoutMs,
  String& resposta
) {
  unsigned long inicio = millis();

  resposta = "";

  while (millis() - inicio < timeoutMs) {
    while (SerialAT.available()) {
      char caractere = SerialAT.read();

      resposta += caractere;
      Serial.write(caractere);

      // O servidor Python sempre responde um JSON terminando em \n.
      if (
        resposta.indexOf("\"comando\":\"INICIAR_FRETE\"") >= 0 ||
        resposta.indexOf("\"comando\":\"AGUARDAR\"") >= 0 ||
        resposta.indexOf("\"comando\":\"TELEMETRIA_CONFIRMADA\"") >= 0 ||
        resposta.indexOf("\"ok\":false") >= 0
      ) {
        return true;
      }
    }
  }

  return resposta.length() > 0;
}

void pausaSemDelay(uint32_t tempoMs) {
  unsigned long inicio = millis();

  while (millis() - inicio < tempoMs) {
    // Mantém a mesma lógica do projeto original sem usar delay longo.
  }
}

// =====================================================
// MODEM E REDE
// =====================================================
bool pulsoPowerKey() {
  Serial.println("Ligando modem pelo PWRKEY...");

  digitalWrite(MODEM_PWRKEY_PIN, LOW);
  pausaSemDelay(1200);
  digitalWrite(MODEM_PWRKEY_PIN, HIGH);

  return true;
}

bool iniciarAHT10() {
  Wire.begin(I2C_SDA, I2C_SCL);

  if (!aht.begin(&Wire, 0, 0x38)) {
    Serial.println("AHT10 nao encontrado.");
    return false;
  }

  Serial.println("AHT10 iniciado.");
  return true;
}

bool lerAHT10(
  float& temperatura,
  float& umidade
) {
  sensors_event_t humidity;
  sensors_event_t temperature;

  if (!aht.getEvent(&humidity, &temperature)) {
    Serial.println("Falha ao ler AHT10.");
    return false;
  }

  temperatura = temperature.temperature;
  umidade = humidity.relative_humidity;

  return true;
}

bool ligarEConectarModem() {
  Serial.println("Verificando modem...");

  if (!modem.testAT(1000L)) {
    pulsoPowerKey();
  }

  Serial.println("Inicializando modem...");

  if (!modem.init()) {
    Serial.println("Falha na comunicacao AT.");
    return false;
  }

  Serial.print("Modelo: ");
  Serial.println(modem.getModemInfo());

  Serial.println("Aguardando rede celular...");

  if (!modem.waitForNetwork(60000L)) {
    Serial.println("Falha ao registrar na rede.");
    return false;
  }

  Serial.println("Rede registrada.");
  Serial.println("Conectando dados da Vivo...");

  if (!modem.gprsConnect(APN, USER, PASS)) {
    Serial.println("Falha ao conectar APN.");
    return false;
  }

  modemConectado = true;

  Serial.print("IP: ");
  Serial.println(modem.localIP());

  return true;
}

bool rede4GAtiva() {
  return (
    modemConectado &&
    modem.isNetworkConnected() &&
    modem.isGprsConnected()
  );
}

bool garantirModemConectado() {
  if (rede4GAtiva()) {
    return true;
  }

  modemConectado = false;

  unsigned long agora = millis();

  if (
    ultimaTentativaRede != 0 &&
    agora - ultimaTentativaRede < INTERVALO_TENTATIVA_REDE_MS
  ) {
    return false;
  }

  ultimaTentativaRede = agora;

  Serial.println("Tentando reconectar o modem e o 4G...");

  return ligarEConectarModem();
}

bool abrirServicoTCP() {
  String resposta;

  if (
    enviarComandoAT(
      "AT+NETOPEN?",
      "+NETOPEN:",
      TIMEOUT_AT_CURTO_MS,
      resposta
    )
  ) {
    if (resposta.indexOf("+NETOPEN: 1") >= 0) {
      return true;
    }
  }

  if (
    !enviarComandoAT(
      "AT+NETOPEN",
      "+NETOPEN:",
      TIMEOUT_AT_MEDIO_MS,
      resposta
    )
  ) {
    Serial.println("Falha ao abrir servico TCP/IP.");
    return false;
  }

  if (resposta.indexOf("+NETOPEN: 0") < 0) {
    Serial.println("NETOPEN nao confirmou sucesso.");
    return false;
  }

  return true;
}

int obterRssiDbm() {
  int csq = modem.getSignalQuality();

  if (csq < 0 || csq == 99 || csq > 31) {
    return 999;
  }

  return -113 + (2 * csq);
}

float obterBateriaVolts() {
  String resposta;

  if (
    !enviarComandoAT(
      "AT+CBC",
      "+CBC:",
      TIMEOUT_AT_CURTO_MS,
      resposta
    )
  ) {
    return -1.0f;
  }

  int inicio = resposta.indexOf("+CBC:");

  if (inicio < 0) {
    return -1.0f;
  }

  inicio += 5;

  while (
    inicio < resposta.length() &&
    (resposta.charAt(inicio) == ' ' || resposta.charAt(inicio) == '\t')
  ) {
    inicio++;
  }

  int fim = inicio;

  while (
    fim < resposta.length() &&
    (
      (resposta.charAt(fim) >= '0' && resposta.charAt(fim) <= '9') ||
      resposta.charAt(fim) == '.'
    )
  ) {
    fim++;
  }

  if (fim == inicio) {
    return -1.0f;
  }

  float volts = resposta.substring(inicio, fim).toFloat();

  if (volts < 2.5f || volts > 5.0f) {
    return -1.0f;
  }

  return volts;
}

int estimarBateriaPercentual(float volts) {
  if (volts < 0.0f) {
    return -1;
  }

  // Estimativa operacional para bateria Li-ion de 1 celula.
  // A tensao tambem e enviada para permitir calibracao futura no backend.
  if (volts >= 4.20f) return 100;
  if (volts >= 4.10f) return 90 + (int)((volts - 4.10f) * 100.0f);
  if (volts >= 4.00f) return 80 + (int)((volts - 4.00f) * 100.0f);
  if (volts >= 3.90f) return 65 + (int)((volts - 3.90f) * 150.0f);
  if (volts >= 3.80f) return 45 + (int)((volts - 3.80f) * 200.0f);
  if (volts >= 3.70f) return 25 + (int)((volts - 3.70f) * 200.0f);
  if (volts >= 3.60f) return 10 + (int)((volts - 3.60f) * 150.0f);
  if (volts >= 3.40f) return (int)((volts - 3.40f) * 50.0f);

  return 0;
}

void fecharSocketTCP() {
  String resposta;

  enviarComandoAT(
    "AT+CIPCLOSE=0",
    "+CIPCLOSE:",
    TIMEOUT_AT_CURTO_MS,
    resposta
  );
}

void resetarConexaoDados() {
  String resposta;

  enviarComandoAT(
    "AT+NETCLOSE",
    "+NETCLOSE:",
    TIMEOUT_AT_MEDIO_MS,
    resposta
  );

  modem.gprsDisconnect();
  modemConectado = false;
}

// =====================================================
// JSON
// =====================================================
String montarPayloadEvento(
  const char* evento
) {
  String json = "{";

  json += "\"device_key\":\"";
  json += DEVICE_KEY;
  json += "\",";

  json += "\"sensor_id\":\"";
  json += SENSOR_ID;
  json += "\",";

  json += "\"evento\":\"";
  json += evento;
  json += "\"";

  json += "}";

  json += "\n";

  return json;
}

String montarPayloadTelemetria(
  float temperatura,
  float umidade,
  bool origemBufferOffline = false
) {
  int rssiDbm = origemBufferOffline ? 999 : obterRssiDbm();
  float bateriaVolts = origemBufferOffline ? -1.0f : obterBateriaVolts();
  int bateriaPercentual = estimarBateriaPercentual(bateriaVolts);

  String json = "{";

  json += "\"device_key\":\"";
  json += DEVICE_KEY;
  json += "\",";

  json += "\"sensor_id\":\"";
  json += SENSOR_ID;
  json += "\",";

  json += "\"evento\":\"telemetria\",";

  json += "\"temperatura\":";
  json += String(temperatura, 2);
  json += ",";

  json += "\"umidade\":";
  json += String(umidade, 2);
  json += ",";

  if (rssiDbm == 999) {
    json += "\"rssi\":null,";
  } else {
    json += "\"rssi\":";
    json += String(rssiDbm);
    json += ",";
  }

  if (bateriaPercentual < 0) {
    json += "\"bateria\":null,";
    json += "\"bateria_volts\":null,";
  } else {
    json += "\"bateria\":";
    json += String(bateriaPercentual);
    json += ",";
    json += "\"bateria_volts\":";
    json += String(bateriaVolts, 3);
    json += ",";
  }

  if (origemBufferOffline) {
    json += "\"status_rede\":\"OFFLINE_BUFFER\",";
    json += "\"origem_buffer\":true,";
    json += "\"capturado_millis\":";
    json += String(millis());
  } else {
    json += "\"status_rede\":\"LTE\",";
    json += "\"origem_buffer\":false";
  }

  json += "}";

  json += "\n";

  return json;
}


// =====================================================
// BUFFER OFFLINE NA FLASH INTERNA - LITTLEFS
// =====================================================
bool iniciarBufferOffline() {
  if (!LittleFS.begin(true)) {
    Serial.println("Falha ao iniciar LittleFS. Buffer offline indisponivel.");
    littleFsOk = false;
    return false;
  }

  littleFsOk = true;

  if (!LittleFS.exists(ARQUIVO_BUFFER)) {
    File arquivo = LittleFS.open(ARQUIVO_BUFFER, FILE_WRITE);

    if (!arquivo) {
      Serial.println("Falha ao criar arquivo do buffer.");
      littleFsOk = false;
      return false;
    }

    arquivo.close();
  }

  File conferirBuffer = LittleFS.open(ARQUIVO_BUFFER, FILE_READ);
  size_t bytesPendentes = conferirBuffer ? conferirBuffer.size() : 0;

  if (conferirBuffer) {
    conferirBuffer.close();
  }

  Serial.print("LittleFS iniciado. Buffer pendente: ");
  Serial.print(bytesPendentes);
  Serial.println(" bytes.");

  return true;
}

void salvarEstadoFreteLocal(bool ativo) {
  if (!littleFsOk) {
    return;
  }

  File arquivo = LittleFS.open(ARQUIVO_ESTADO_FRETE, FILE_WRITE);

  if (!arquivo) {
    Serial.println("Falha ao salvar estado local do frete.");
    return;
  }

  arquivo.print(ativo ? "1" : "0");
  arquivo.close();
}

bool carregarEstadoFreteLocal() {
  if (!littleFsOk || !LittleFS.exists(ARQUIVO_ESTADO_FRETE)) {
    return false;
  }

  File arquivo = LittleFS.open(ARQUIVO_ESTADO_FRETE, FILE_READ);

  if (!arquivo) {
    return false;
  }

  String valor = arquivo.readString();
  arquivo.close();
  valor.trim();

  return valor == "1";
}

size_t tamanhoBufferOffline() {
  if (!littleFsOk || !LittleFS.exists(ARQUIVO_BUFFER)) {
    return 0;
  }

  File arquivo = LittleFS.open(ARQUIVO_BUFFER, FILE_READ);

  if (!arquivo) {
    return 0;
  }

  size_t tamanho = arquivo.size();
  arquivo.close();

  return tamanho;
}

uint32_t contarRegistrosBufferOffline() {
  if (!littleFsOk || !LittleFS.exists(ARQUIVO_BUFFER)) {
    return 0;
  }

  File arquivo = LittleFS.open(ARQUIVO_BUFFER, FILE_READ);

  if (!arquivo) {
    return 0;
  }

  uint32_t quantidade = 0;

  while (arquivo.available()) {
    String linha = arquivo.readStringUntil('\n');
    linha.trim();

    if (linha.length() > 0) {
      quantidade++;
    }
  }

  arquivo.close();

  return quantidade;
}

bool salvarPayloadNoBuffer(const String& payload) {
  if (!littleFsOk) {
    Serial.println("LittleFS indisponivel. Leitura offline nao foi salva.");
    return false;
  }

  size_t tamanhoAtual = tamanhoBufferOffline();

  if (tamanhoAtual + payload.length() > LIMITE_BUFFER_BYTES) {
    Serial.println("Buffer offline atingiu o limite de seguranca.");
    Serial.println("A leitura nova nao foi gravada para proteger a flash.");
    return false;
  }

  File arquivo = LittleFS.open(ARQUIVO_BUFFER, FILE_APPEND);

  if (!arquivo) {
    Serial.println("Falha ao abrir buffer offline para escrita.");
    return false;
  }

  arquivo.print(payload);

  // Garante uma linha NDJSON por registro.
  if (!payload.endsWith("\n")) {
    arquivo.print("\n");
  }

  arquivo.flush();
  arquivo.close();

  Serial.print("Leitura salva no buffer offline. Pendentes: ");
  Serial.println(contarRegistrosBufferOffline());

  return true;
}

String lerPrimeiroRegistroBuffer() {
  if (!littleFsOk || !LittleFS.exists(ARQUIVO_BUFFER)) {
    return "";
  }

  File arquivo = LittleFS.open(ARQUIVO_BUFFER, FILE_READ);

  if (!arquivo) {
    return "";
  }

  String linha = "";

  while (arquivo.available() && linha.length() == 0) {
    linha = arquivo.readStringUntil('\n');
    linha.trim();
  }

  arquivo.close();

  if (linha.length() > 0) {
    linha += "\n";
  }

  return linha;
}

bool removerPrimeiroRegistroBuffer() {
  if (!littleFsOk || !LittleFS.exists(ARQUIVO_BUFFER)) {
    return false;
  }

  File origem = LittleFS.open(ARQUIVO_BUFFER, FILE_READ);

  if (!origem) {
    return false;
  }

  LittleFS.remove(ARQUIVO_BUFFER_TEMP);

  File destino = LittleFS.open(ARQUIVO_BUFFER_TEMP, FILE_WRITE);

  if (!destino) {
    origem.close();
    return false;
  }

  bool primeiraLinhaRemovida = false;

  while (origem.available()) {
    String linha = origem.readStringUntil('\n');
    linha.trim();

    if (linha.length() == 0) {
      continue;
    }

    if (!primeiraLinhaRemovida) {
      primeiraLinhaRemovida = true;
      continue;
    }

    destino.println(linha);
  }

  origem.close();
  destino.flush();
  destino.close();

  if (!primeiraLinhaRemovida) {
    LittleFS.remove(ARQUIVO_BUFFER_TEMP);
    return false;
  }

  LittleFS.remove(ARQUIVO_BUFFER);

  if (!LittleFS.rename(ARQUIVO_BUFFER_TEMP, ARQUIVO_BUFFER)) {
    Serial.println("Falha ao substituir arquivo do buffer.");
    return false;
  }

  return true;
}

bool arquivarBufferSemFreteAtivo() {
  if (
    !littleFsOk ||
    !LittleFS.exists(ARQUIVO_BUFFER) ||
    contarRegistrosBufferOffline() == 0
  ) {
    return true;
  }

  File origem = LittleFS.open(ARQUIVO_BUFFER, FILE_READ);

  if (!origem) {
    Serial.println("Falha ao abrir buffer para arquivamento.");
    return false;
  }

  File destino = LittleFS.open(ARQUIVO_BUFFER_ORFAO, FILE_APPEND);

  if (!destino) {
    origem.close();
    Serial.println("Falha ao abrir arquivo de buffer orfao.");
    return false;
  }

  uint32_t movidos = 0;

  while (origem.available()) {
    String linha = origem.readStringUntil('\n');
    linha.trim();

    if (linha.length() == 0) {
      continue;
    }

    destino.println(linha);
    movidos++;
  }

  origem.close();
  destino.flush();
  destino.close();

  // Limpa somente a fila ativa. O arquivo órfão continua preservado.
  File limpar = LittleFS.open(ARQUIVO_BUFFER, FILE_WRITE);

  if (limpar) {
    limpar.close();
  } else {
    Serial.println("Falha ao limpar fila ativa depois do arquivamento.");
    return false;
  }

  Serial.print("Registros movidos para buffer orfao: ");
  Serial.println(movidos);

  return true;
}


// =====================================================
// TCP
// =====================================================
bool transacaoTCP(
  const String& payload,
  uint32_t timeoutRespostaMs,
  String& respostaServidor
) {
  respostaServidor = "";

  if (!garantirModemConectado()) {
    return false;
  }

  if (!abrirServicoTCP()) {
    return false;
  }

  if (
    !enviarComandoAT(
      "AT+CIPRXGET=0",
      "OK",
      TIMEOUT_AT_CURTO_MS
    )
  ) {
    Serial.println("Falha ao configurar recebimento TCP.");
    return false;
  }

  String respostaAbertura;

  String comandoAbrir =
    "AT+CIPOPEN=0,\"TCP\",\"" +
    String(TCP_SERVER) +
    "\"," +
    String(TCP_PORT);

  if (
    !enviarComandoAT(
      comandoAbrir,
      "+CIPOPEN:",
      TIMEOUT_SOCKET_MS,
      respostaAbertura
    )
  ) {
    Serial.println("Falha ao abrir socket TCP.");
    return false;
  }

  if (respostaAbertura.indexOf("+CIPOPEN: 0,0") < 0) {
    Serial.println("Socket TCP nao foi conectado.");
    Serial.println("Resposta do modem:");
    Serial.println(respostaAbertura);

    return false;
  }

  Serial.println();
  Serial.println("JSON TCP enviado:");
  Serial.print(payload);

  limparBufferModem();

  String comandoEnvio =
    "AT+CIPSEND=0," +
    String(payload.length());

  Serial.print("\n>> ");
  Serial.println(comandoEnvio);

  SerialAT.println(comandoEnvio);

  String respostaPrompt;

  if (
    !aguardarPromptTCP(
      TIMEOUT_AT_CURTO_MS,
      respostaPrompt
    )
  ) {
    Serial.println("Prompt de envio TCP nao recebido.");
    fecharSocketTCP();
    return false;
  }

  SerialAT.print(payload);

  String respostaEnvio;

  if (
    !esperarResposta(
      "+CIPSEND:",
      TIMEOUT_AT_MEDIO_MS,
      respostaEnvio
    )
  ) {
    Serial.println("Falha ou timeout no envio TCP.");
    fecharSocketTCP();
    return false;
  }

  String confirmacaoEsperada =
    "+CIPSEND: 0," +
    String(payload.length()) +
    "," +
    String(payload.length());

  bool envioConfirmado =
    respostaEnvio.indexOf(confirmacaoEsperada) >= 0;

  if (!envioConfirmado) {
    Serial.println("O modem nao confirmou todos os bytes.");
    Serial.println("Resposta:");
    Serial.println(respostaEnvio);

    fecharSocketTCP();
    return false;
  }

  bool recebeuResposta = aguardarRespostaServidor(
    timeoutRespostaMs,
    respostaServidor
  );

  fecharSocketTCP();

  if (!recebeuResposta) {
    Serial.println("Servidor nao respondeu dentro do timeout.");
    return false;
  }

  Serial.println();
  Serial.println("Resposta do servidor:");
  Serial.println(respostaServidor);

  return true;
}

bool respostaConfirmaTelemetria(const String& resposta) {
  return (
    resposta.indexOf("\"comando\":\"TELEMETRIA_CONFIRMADA\"") >= 0 ||
    resposta.indexOf("\"ok\":true") >= 0
  );
}

bool reenviarUmRegistroDoBuffer() {
  if (!littleFsOk) {
    return true;
  }

  String payload = lerPrimeiroRegistroBuffer();

  if (payload.length() == 0) {
    return true;
  }

  Serial.println();
  Serial.println("Reenviando um registro do buffer offline...");

  String resposta;

  bool ok = transacaoTCP(
    payload,
    TIMEOUT_AT_MEDIO_MS,
    resposta
  );

  if (!ok || !respostaConfirmaTelemetria(resposta)) {
    Serial.println("Registro do buffer ainda nao foi confirmado.");
    return false;
  }

  if (!removerPrimeiroRegistroBuffer()) {
    Serial.println("Servidor confirmou, mas houve falha ao remover o registro local.");
    return false;
  }

  Serial.print("Registro offline confirmado e removido. Pendentes: ");
  Serial.println(contarRegistrosBufferOffline());

  return true;
}

void reenviarBufferNoCicloOnline() {
  for (
    uint8_t tentativa = 0;
    tentativa < MAX_REENVIO_BUFFER_POR_CICLO;
    tentativa++
  ) {
    if (contarRegistrosBufferOffline() == 0) {
      return;
    }

    if (!reenviarUmRegistroDoBuffer()) {
      return;
    }
  }
}

// =====================================================
// LÓGICA DE FRETE
// =====================================================
bool aguardarSinalDeFrete() {
  String resposta;

  Serial.println();
  Serial.println("Aguardando sinal de inicio de frete pela VM...");

  String payload = montarPayloadEvento("aguardar_frete");

  bool ok = transacaoTCP(
    payload,
    TIMEOUT_AGUARDAR_FRETE_MS,
    resposta
  );

  if (!ok) {
    Serial.println("Falha ao aguardar sinal de frete.");
    resetarConexaoDados();
    return false;
  }

  if (
    resposta.indexOf("\"comando\":\"INICIAR_FRETE\"") >= 0 ||
    resposta.indexOf("\"frete_ativo\":true") >= 0
  ) {
    Serial.println("Frete ativo detectado. Sensor liberado para leitura.");
    return true;
  }

  Serial.println("Nenhum frete ativo ainda.");
  return false;
}

bool consultarFreteAtivoRapido() {
  String resposta;

  String payload = montarPayloadEvento("status_frete");

  bool ok = transacaoTCP(
    payload,
    TIMEOUT_AT_MEDIO_MS,
    resposta
  );

  if (!ok) {
    Serial.println("Falha ao consultar status do frete.");
    resetarConexaoDados();
    return freteAtivo;
  }

  return (
    resposta.indexOf("\"comando\":\"INICIAR_FRETE\"") >= 0 ||
    resposta.indexOf("\"frete_ativo\":true") >= 0
  );
}

bool enviarTelemetriaTCP(
  float temperatura,
  float umidade
) {
  String resposta;

  String payload = montarPayloadTelemetria(
    temperatura,
    umidade
  );

  bool ok = transacaoTCP(
    payload,
    TIMEOUT_AT_MEDIO_MS,
    resposta
  );

  if (!ok) {
    Serial.println("Falha no envio da telemetria.");
    resetarConexaoDados();
    return false;
  }

  if (respostaConfirmaTelemetria(resposta)) {
    Serial.println("Servidor Python confirmou a gravacao.");
    return true;
  }

  Serial.println("Envio confirmado pelo modem, mas servidor nao confirmou ok.");
  return false;
}

// =====================================================
// SETUP
// =====================================================
void setup() {
  Serial.begin(115200);
  pausaSemDelay(1000);

  Serial.println();
  Serial.println("====================================================");
  Serial.println("TRUSTFLOW - A7670E + AHT10 + TCP LONG POLLING");
  Serial.println("====================================================");

  Serial.print("Sensor ID: ");
  Serial.println(SENSOR_ID);

  Serial.print("Servidor TCP: ");
  Serial.print(TCP_SERVER);
  Serial.print(":");
  Serial.println(TCP_PORT);

  if (!iniciarAHT10()) {
    Serial.println("Sistema parado: AHT10 indisponivel.");

    while (true) {
      pausaSemDelay(1000);
    }
  }

  iniciarBufferOffline();

  // Recupera o ultimo estado conhecido.
  // Isso permite continuar registrando offline depois de um reboot,
  // caso o dispositivo tenha perdido o 4G durante um frete ativo.
  freteAtivo = carregarEstadoFreteLocal();

  Serial.print("Estado local recuperado - frete ativo: ");
  Serial.println(freteAtivo ? "SIM" : "NAO");

  if (!freteAtivo && contarRegistrosBufferOffline() > 0) {
    Serial.println(
      "Buffer pendente encontrado sem frete ativo local. "
      "Arquivando para nao associar a uma nova viagem."
    );
    arquivarBufferSemFreteAtivo();
  }

  iniciarPinosModem();

  SerialAT.begin(
    115200,
    SERIAL_8N1,
    MODEM_RX_PIN,
    MODEM_TX_PIN
  );

  if (!ligarEConectarModem()) {
    Serial.println("Falha inicial de rede.");
    Serial.println("O loop continuara tentando reconectar.");
  }

  ultimoEnvioOnline = 0;
  ultimaGravacaoOffline = 0;
  rede4GAnterior = rede4GAtiva();
}

// =====================================================
// LOOP
// =====================================================
void loop() {
  bool online = garantirModemConectado();
  unsigned long agora = millis();

  if (online && !rede4GAnterior) {
    Serial.println("4G restabelecido.");
    ultimoEnvioOnline = 0;
  }

  if (!online && rede4GAnterior) {
    Serial.println("4G indisponivel. Entrando em modo de buffer offline.");
    ultimaGravacaoOffline = 0;
  }

  rede4GAnterior = online;

  // REGRA DE NEGOCIO:
  // O frete precisa iniciar com o dispositivo online.
  // Sem um frete ativo conhecido, a placa precisa da VM para receber
  // o comando INICIAR_FRETE. Portanto, sem 4G ela apenas aguarda
  // e NAO coleta dados offline.
  if (!freteAtivo) {
    if (!online) {
      Serial.println("Sem 4G e sem frete ativo conhecido.");
      pausaSemDelay(5000);
      return;
    }

    freteAtivo = aguardarSinalDeFrete();

    if (!freteAtivo) {
      pausaSemDelay(PAUSA_SEM_FRETE_MS);
      return;
    }

    salvarEstadoFreteLocal(true);
    ultimoEnvioOnline = 0;
    ultimaGravacaoOffline = 0;
  }

  // ===================================================
  // MODO ONLINE - 4G DISPONIVEL
  // Envia telemetria atual a cada 1 minuto.
  // No mesmo ciclo, tenta reenviar um registro do buffer.
  // ===================================================
  if (online) {
    if (
      ultimoEnvioOnline != 0 &&
      agora - ultimoEnvioOnline < INTERVALO_ENVIO_ONLINE_MS
    ) {
      pausaSemDelay(250);
      return;
    }

    ultimoEnvioOnline = agora;

    // Confirma se o frete continua ativo antes de enviar.
    bool continuaAtivo = consultarFreteAtivoRapido();

    if (!continuaAtivo) {
      Serial.println("Frete finalizado ou inexistente. Parando leituras.");
      freteAtivo = false;
      salvarEstadoFreteLocal(false);
      ultimoEnvioOnline = 0;
      ultimaGravacaoOffline = 0;

      if (contarRegistrosBufferOffline() > 0) {
        Serial.println(
          "Existem registros offline pendentes. "
          "Eles serao arquivados para nao entrar em um novo frete."
        );
        arquivarBufferSemFreteAtivo();
      }

      return;
    }

    // Primeiro drena um registro antigo por minuto.
    reenviarBufferNoCicloOnline();

    float temperatura = 0;
    float umidade = 0;

    if (!lerAHT10(temperatura, umidade)) {
      Serial.println("Falha ao ler AHT10. Tentando novamente depois.");
      return;
    }

    Serial.println();
    Serial.print("Temperatura online: ");
    Serial.print(temperatura, 2);
    Serial.println(" C");

    Serial.print("Umidade online: ");
    Serial.print(umidade, 2);
    Serial.println(" %");

    bool enviado = enviarTelemetriaTCP(
      temperatura,
      umidade
    );

    if (enviado) {
      Serial.println("Telemetria online enviada.");
      return;
    }

    Serial.println("Falha no envio online.");

    // Evita gravar na flash a cada minuto.
    // Mesmo após uma falha de envio, o buffer respeita 5 minutos.
    if (
      ultimaGravacaoOffline == 0 ||
      agora - ultimaGravacaoOffline >= INTERVALO_BUFFER_OFFLINE_MS
    ) {
      String payloadOffline = montarPayloadTelemetria(
        temperatura,
        umidade,
        true
      );

      salvarPayloadNoBuffer(payloadOffline);
      ultimaGravacaoOffline = agora;
    }

    return;
  }

  // ===================================================
  // MODO OFFLINE - SEM 4G
  // Com frete ativo conhecido, salva a cada 5 minutos.
  // ===================================================
  if (
    ultimaGravacaoOffline != 0 &&
    agora - ultimaGravacaoOffline < INTERVALO_BUFFER_OFFLINE_MS
  ) {
    pausaSemDelay(500);
    return;
  }

  ultimaGravacaoOffline = agora;

  float temperatura = 0;
  float umidade = 0;

  if (!lerAHT10(temperatura, umidade)) {
    Serial.println("Falha ao ler AHT10 no modo offline.");
    return;
  }

  Serial.println();
  Serial.print("Temperatura offline: ");
  Serial.print(temperatura, 2);
  Serial.println(" C");

  Serial.print("Umidade offline: ");
  Serial.print(umidade, 2);
  Serial.println(" %");

  String payloadOffline = montarPayloadTelemetria(
    temperatura,
    umidade,
    true
  );

  salvarPayloadNoBuffer(payloadOffline);
}
