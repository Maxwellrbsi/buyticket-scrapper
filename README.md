# BuyTicket Brasil — Web Scraper

Extrai todos os eventos de `https://buyticketbrasil.com/eventos` e salva em `eventos.json` com dados detalhados por data.

---

## Como funciona

O site é uma SPA construída em Bubble.io que não renderiza HTML estático — todo o conteúdo chega via chamadas AJAX a um endpoint Elasticsearch interno. O scraper aproveita isso para extrair dados diretamente da API, sem precisar analisar o DOM.

### Fase 1 — Listagem

1. Abre `https://buyticketbrasil.com/eventos` com Playwright (Chromium headless).
2. Instala um listener de respostas HTTP que intercepta todas as chamadas ao endpoint `/elasticsearch/search` e `/elasticsearch/msearch`.
3. Rola a página até o fim, disparando a paginação da API (Bubble carrega ~25 eventos por batch via scroll).
4. Coleta os `_source` de cada hit, deduplica por `_id` e normaliza os campos.
5. Salva a primeira versão do `eventos.json` (sem detalhes por data).

### Fase 2 — Enrichment (detalhes por data)

6. Para cada evento, abre sua página `/datas/<slug>` em paralelo (padrão: 8 abas simultâneas).
7. Intercepta as respostas da mesma API buscando o campo `json_datas_text` — uma string JSON embutida com detalhes granulares por data (cidade, tipo, ingressos disponíveis, etc.).
8. Faz salvamento incremental a cada 20 eventos para recuperação em caso de falha.
9. Salva a versão final do `eventos.json` com `datas_detalhadas` populado.

---

## Pré-requisitos

- Python 3.9+
- Acesso à internet

## Instalação

```bash
pip install -r requirements.txt
playwright install chromium
```

> No Windows, se `python` não funcionar, use `py` (o Python Launcher).

---

## Como rodar

```bash
python scraper.py
```

### Variáveis de ambiente opcionais

| Variável      | Padrão | Descrição                                                   |
|---------------|--------|-------------------------------------------------------------|
| `SKIP_ENRICH` | `0`    | Se `1`, pula a fase 2 (enrichment) — retorna só a listagem  |
| `LIMIT`       | `0`    | Se > 0, enriquece apenas os primeiros N eventos             |
| `CONCURRENCY` | `8`    | Número de abas paralelas no enrichment                      |

**Exemplos:**

```bash
# Apenas listagem, sem visitar cada evento:
SKIP_ENRICH=1 python scraper.py

# Testar com os primeiros 10 eventos:
LIMIT=10 python scraper.py

# Aumentar paralelismo (máquinas mais potentes):
CONCURRENCY=16 python scraper.py
```

> No Windows (PowerShell), use `$env:SKIP_ENRICH=1; python scraper.py` ou prefixe com `set SKIP_ENRICH=1 &&`.

---

## Output

Gera `eventos.json` na raiz do projeto — um array JSON com ~400 objetos, ordenado por data (próximos eventos primeiro, passados depois).

### Tempo de execução esperado

| Modo                       | Tempo aproximado |
|----------------------------|-----------------|
| Só listagem (`SKIP_ENRICH=1`) | ~30 segundos  |
| Listagem + enrichment completo | ~8–12 minutos |

---

## Schema do JSON

Cada item do array segue esta estrutura:

```json
{
  "nome": "Roxette Live! - 2026",
  "slug": "roxettelive-2026",
  "url": "https://buyticketbrasil.com/datas/roxettelive-2026",
  "imagem_url": "https://2da743d0613562afbb5c3a87cfff928c.cdn.bubble.io/...",
  "descricao": "Texto completo da descrição (BBCode removido)",
  "descricao_raw": "Texto original com [b]tags[/b] do BBCode",
  "categoria": "pop",
  "tipo_categoria": "musical",
  "tipo_evento": "evento_grande",
  "importancia": "alta",
  "ticketeira": "eventim",
  "estados": ["RJ", "SP"],
  "metodos_pagamento": ["pix", "cart_o_de_cr_dito"],
  "transferivel": "Transferível",
  "total_ingressos": 15,
  "evento_passou": false,
  "desabilitado": false,
  "ingresso_liberado": true,
  "primeira_data": "2026-04-13T21:00:00+00:00",
  "ultima_data": "2026-04-13T21:00:00+00:00",
  "datas": ["2026-04-13T21:00:00+00:00"],
  "datas_timestamps_ms": [1776359100000],
  "_id": "1773234789043x912345678901234567",
  "datas_detalhadas": [
    {
      "data_evento": "2026-04-13T01:00:00.000Z",
      "cidade": "Rio de Janeiro",
      "tipo": "Normal",
      "ingressos_disponiveis": 7,
      "quantidade_tipos_entrada": 7,
      "compra_venda_desabilitadas": false,
      "evento_venda_desabilitada": false,
      "local": "",
      "time1": "",
      "time2": "",
      "id_local": "1773235096197x451119925810954200",
      "id_event": "1773235061217x914258766866677800"
    }
  ]
}
```

### Campos raiz

| Campo                  | Tipo             | Descrição                                              |
|------------------------|------------------|--------------------------------------------------------|
| `nome`                 | string           | Nome do evento                                         |
| `slug`                 | string           | Identificador na URL                                   |
| `url`                  | string           | URL completa da página do evento                       |
| `imagem_url`           | string           | URL do flyer/imagem de capa                            |
| `descricao`            | string           | Descrição sem BBCode                                   |
| `descricao_raw`        | string           | Descrição original (com `[b]`, `[i]`, etc.)            |
| `categoria`            | string           | Categoria principal (ex: `pop`, `sertanejo`)           |
| `tipo_categoria`       | string           | Tipo (ex: `musical`, `teatro`)                         |
| `tipo_evento`          | string           | Porte (ex: `evento_grande`, `show_pequeno`)            |
| `importancia`          | string           | Relevância interna do evento                           |
| `ticketeira`           | string           | Plataforma de venda (ex: `eventim`, `ingressorapido`)  |
| `estados`              | string[]         | Estados onde o evento ocorre (ex: `["SP", "RJ"]`)      |
| `metodos_pagamento`    | string[]         | Formas de pagamento aceitas                            |
| `transferivel`         | string           | Se o ingresso é transferível                           |
| `total_ingressos`      | number           | Total de ingressos cadastrados                         |
| `evento_passou`        | boolean          | Se todas as datas já ocorreram                         |
| `desabilitado`         | boolean          | Se o evento está desabilitado na plataforma            |
| `ingresso_liberado`    | boolean          | Se a venda está liberada                               |
| `primeira_data`        | string (ISO 8601)| Data mais próxima do evento                            |
| `ultima_data`          | string (ISO 8601)| Data mais distante do evento                           |
| `datas`                | string[]         | Todas as datas (ISO 8601, UTC)                         |
| `datas_timestamps_ms`  | number[]         | Datas como timestamp Unix em ms (fonte: Bubble)        |
| `_id`                  | string           | ID interno do Bubble                                   |
| `datas_detalhadas`     | object[]         | Detalhes por data (ver abaixo)                         |

### Campos de `datas_detalhadas[i]`

| Campo                      | Tipo    | Descrição                                           |
|----------------------------|---------|-----------------------------------------------------|
| `data_evento`              | string  | Data/hora da sessão (ISO 8601, UTC)                 |
| `cidade`                   | string  | Cidade onde ocorre a sessão                         |
| `tipo`                     | string  | `"Normal"` ou `"Passe"` (pass de múltiplos dias)   |
| `ingressos_disponiveis`    | number  | Quantidade de ingressos ainda disponíveis           |
| `quantidade_tipos_entrada` | number  | Número de tipos de entrada (ex: pista, VIP, camarote) |
| `compra_venda_desabilitadas` | boolean | Se compra e venda estão bloqueadas para esta data  |
| `evento_venda_desabilitada`  | boolean | Se a venda do evento como um todo está bloqueada   |
| `local`                    | string  | Nome do local/venue (quando disponível)             |
| `time1`                    | string  | Time/artista 1 (para eventos esportivos)            |
| `time2`                    | string  | Time/artista 2 (para eventos esportivos)            |
| `id_local`                 | string  | ID interno do local no Bubble                       |
| `id_event`                 | string  | ID interno da sessão no Bubble                      |

---

## Observações

- Datas estão em UTC. O site exibe no fuso de São Paulo (BRT = UTC-3).
- `descricao` remove tags BBCode simples (`[b]`, `[i]`, `[u]`, `[url]`, `[img]`); `descricao_raw` preserva o original.
- A ordenação do array prioriza eventos futuros (pelo `min(datas_futuras)`), depois passados (pelo `max(datas)` decrescente).
- O salvamento incremental no enrichment garante que o `eventos.json` sempre contenha os dados mais recentes mesmo em caso de interrupção.
