"""
Scraper de eventos do site https://buyticketbrasil.com/eventos
-----------------------------------------------------------------

Estrategia:
  O site e uma SPA Bubble.io que carrega os eventos via chamadas AJAX
  para o endpoint interno /elasticsearch/search. Em vez de percorrer o
  DOM evento por evento (lento), interceptamos essas respostas JSON e
  extraimos todos os campos (nome, slug, imagem, datas, descricao,
  estados, categoria, etc.) diretamente.

Fluxo:
  1. Abre https://buyticketbrasil.com/eventos com Playwright (headless).
  2. Escuta os responses do dominio buyticketbrasil.com em /elasticsearch/.
  3. Faz scroll da pagina para disparar paginacao ate que nao cheguem
     mais eventos novos.
  4. Deduplica por slug, normaliza campos em eventos.
  5. (Enrichment) Visita cada /datas/<slug> em paralelo e captura o
     campo json_datas_text (detalhe por data: cidade, tipo Normal/Passe,
     ingressos disponiveis por data, etc.).
  6. Salva em eventos.json.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright
from playwright.sync_api import (
    Page,
    Response,
    TimeoutError as PWTimeoutError,
    sync_playwright,
)


BASE_URL = "https://buyticketbrasil.com"
LISTING_URL = f"{BASE_URL}/eventos"
IMAGE_CDN_PREFIX = "https://2da743d0613562afbb5c3a87cfff928c.cdn.bubble.io"
PROJECT_DIR = Path(__file__).parent
OUTPUT_FILE = PROJECT_DIR / "eventos.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

CARD_SELECTOR = ".bubble-element.group-item"


# ---------------------------------------------------------------------------
# Normalizacao dos campos da API
# ---------------------------------------------------------------------------

def _ts_to_iso(ts_ms: Any) -> str:
    """Timestamp Bubble (ms) -> ISO 8601 em America/Sao_Paulo (UTC-3)."""
    if not isinstance(ts_ms, (int, float)):
        return ""
    try:
        # Bubble usa UTC; convertemos para data local conhecida (BRT = UTC-3).
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        return dt.isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _flyer_url(flyer: str) -> str:
    """Normaliza o caminho do flyer para URL absoluta."""
    if not flyer:
        return ""
    if flyer.startswith("//"):
        return "https:" + flyer
    if flyer.startswith("/"):
        return IMAGE_CDN_PREFIX + flyer
    return flyer


def _limpar_bbcode(texto: str) -> str:
    """Remove tags simples tipo [b]...[/b] deixando o texto plano."""
    if not texto:
        return ""
    out = texto
    for tag in ("b", "i", "u", "s", "url", "img"):
        out = out.replace(f"[{tag}]", "").replace(f"[/{tag}]", "")
    return out


def normalize_event(src: dict[str, Any]) -> dict[str, Any]:
    """Converte um _source do Elasticsearch no schema final."""
    slug = src.get("Slug", "")
    datas_raw = src.get("datas_list_date") or []
    datas_iso = [_ts_to_iso(t) for t in datas_raw if _ts_to_iso(t)]

    return {
        "nome": src.get("nome_text", ""),
        "slug": slug,
        "url": f"{BASE_URL}/datas/{slug}" if slug else "",
        "imagem_url": _flyer_url(src.get("flyer_image", "")),
        "descricao": _limpar_bbcode(src.get("descricao_text", "")),
        "descricao_raw": src.get("descricao_text", ""),
        "categoria": src.get("categoria_option_categoria", ""),
        "tipo_categoria": src.get("tipo_categoria_option_tipo_categoria", ""),
        "tipo_evento": src.get("tipo_evento_musical_option_tipo_eventos_musicais", ""),
        "importancia": src.get("importancia_option_importancia_evento", ""),
        "ticketeira": src.get("ticketeira_option_os__ticketeiras", ""),
        "estados": src.get("locais_text_list_text") or [],
        "metodos_pagamento": src.get("metodos_pagamentos_list_option_metodo_pagamento") or [],
        "transferivel": src.get("transferivel_text", ""),
        "total_ingressos": src.get("total_ingressos_number", 0),
        "evento_passou": bool(src.get("is_evento_passou_boolean", False)),
        "desabilitado": bool(src.get("is_desabilitado_boolean", False)),
        "ingresso_liberado": bool(src.get("is_ingresso_liberado_boolean", False)),
        "primeira_data": _ts_to_iso(src.get("primeira_data_disponivel_date")),
        "ultima_data": _ts_to_iso(src.get("ultima_data_disponivel_date")),
        "datas": datas_iso,
        "datas_timestamps_ms": datas_raw,
        "_id": src.get("_id", ""),
    }


# ---------------------------------------------------------------------------
# Interceptacao e coleta
# ---------------------------------------------------------------------------

class EventCollector:
    def __init__(self) -> None:
        self.by_id: dict[str, dict[str, Any]] = {}
        self.raw_sources: list[dict[str, Any]] = []

    def handle_response(self, response: Response) -> None:
        url = response.url
        if "buyticketbrasil.com/elasticsearch/" not in url:
            return
        try:
            data = response.json()
        except Exception:
            return
        hits = self._extract_hits(data)
        for hit in hits:
            src = hit.get("_source") or {}
            evento_id = hit.get("_id") or src.get("_id") or src.get("Slug")
            if not evento_id:
                continue
            if "nome_text" not in src and "Slug" not in src:
                continue
            if evento_id in self.by_id:
                continue
            src_with_id = {**src, "_id": src.get("_id", evento_id)}
            self.by_id[evento_id] = src_with_id
            self.raw_sources.append(src_with_id)

    @staticmethod
    def _extract_hits(data: Any) -> list[dict[str, Any]]:
        """O endpoint retorna diferentes formatos; coletamos todos os hits."""
        hits: list[dict[str, Any]] = []
        if isinstance(data, dict):
            h = data.get("hits")
            if isinstance(h, dict):
                inner = h.get("hits")
                if isinstance(inner, list):
                    hits.extend(inner)
            # responses[].hits.hits (msearch)
            resps = data.get("responses")
            if isinstance(resps, list):
                for r in resps:
                    if isinstance(r, dict):
                        rh = r.get("hits")
                        if isinstance(rh, dict):
                            ri = rh.get("hits")
                            if isinstance(ri, list):
                                hits.extend(ri)
        return hits

    def count(self) -> int:
        return len(self.by_id)


# ---------------------------------------------------------------------------
# Enrichment: visita cada pagina /datas/<slug> e captura json_datas_text
# ---------------------------------------------------------------------------

def _parse_json_datas_text(valor: str) -> list[dict[str, Any]]:
    """Parseia a string JSON do campo json_datas_text em lista de dicts."""
    if not valor:
        return []
    try:
        arr = json.loads(valor)
    except Exception:
        return []
    if not isinstance(arr, list):
        return []
    out = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        out.append({
            "data_evento": item.get("data_evento", ""),
            "cidade": item.get("cidade", ""),
            "tipo": item.get("tipo", ""),
            "ingressos_disponiveis": item.get("ingressos_disponiveis", 0),
            "quantidade_tipos_entrada": item.get("quantidade_tipos_entrada", 0),
            "compra_venda_desabilitadas": bool(item.get("compra_venda_desabilitadas", False)),
            "evento_venda_desabilitada": bool(item.get("evento_venda_desabilitada", False)),
            "local": item.get("local", ""),
            "time1": item.get("time1", ""),
            "time2": item.get("time2", ""),
            "id_local": item.get("id_local", ""),
            "id_event": item.get("idEvent", ""),
        })
    return out


def _find_json_datas_text(body: Any) -> str | None:
    """Varre recursivamente uma resposta procurando o primeiro json_datas_text."""
    if isinstance(body, dict):
        if "json_datas_text" in body and isinstance(body["json_datas_text"], str):
            return body["json_datas_text"]
        for v in body.values():
            r = _find_json_datas_text(v)
            if r is not None:
                return r
    elif isinstance(body, list):
        for v in body:
            r = _find_json_datas_text(v)
            if r is not None:
                return r
    return None


async def _enrich_one(context, url: str, sem: asyncio.Semaphore) -> list[dict[str, Any]]:
    """Abre uma aba, aguarda json_datas_text em alguma resposta, retorna a lista."""
    async with sem:
        page = await context.new_page()
        result: list[dict[str, Any]] = []
        loop = asyncio.get_event_loop()
        future: asyncio.Future[str] = loop.create_future()

        async def on_response(resp):
            if future.done():
                return
            u = resp.url
            if "buyticketbrasil.com" not in u or "/elasticsearch/" not in u:
                return
            try:
                body = await resp.json()
            except Exception:
                return
            valor = _find_json_datas_text(body)
            if valor is not None and not future.done():
                future.set_result(valor)

        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                valor = await asyncio.wait_for(future, timeout=15)
                result = _parse_json_datas_text(valor)
            except asyncio.TimeoutError:
                result = []
        except Exception:
            result = []
        finally:
            try:
                await page.close()
            except Exception:
                pass

        return result


async def enrich_async(eventos: list[dict[str, Any]], concurrency: int = 8) -> None:
    sem = asyncio.Semaphore(concurrency)
    progresso = {"feitos": 0, "com_detalhes": 0}
    total = len(eventos)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 900},
            locale="pt-BR",
        )

        async def worker(ev: dict[str, Any]) -> None:
            url = ev.get("url", "")
            if not url:
                ev["datas_detalhadas"] = []
                return
            datas_det = await _enrich_one(context, url, sem)
            ev["datas_detalhadas"] = datas_det
            progresso["feitos"] += 1
            if datas_det:
                progresso["com_detalhes"] += 1
            if progresso["feitos"] % 20 == 0 or progresso["feitos"] == total:
                print(f"[enrich] {progresso['feitos']}/{total} "
                      f"(com detalhes: {progresso['com_detalhes']})")
                # Salvamento incremental.
                OUTPUT_FILE.write_text(
                    json.dumps(eventos, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

        await asyncio.gather(*(worker(ev) for ev in eventos))
        await context.close()
        await browser.close()


def enrich_events(eventos: list[dict[str, Any]]) -> None:
    """Wrapper sync para a etapa de enrichment assincrona."""
    concurrency = int(os.environ.get("CONCURRENCY", "8") or 8)
    print(f"[enrich] iniciando com concorrencia={concurrency}")
    asyncio.run(enrich_async(eventos, concurrency=concurrency))


# ---------------------------------------------------------------------------
# Scroll para forcar paginacao
# ---------------------------------------------------------------------------

def _wait_listing_ready(page: Page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeoutError:
        pass
    try:
        page.wait_for_selector(CARD_SELECTOR, timeout=15000)
    except PWTimeoutError:
        print("[warn] cards nao apareceram no DOM")


def scroll_until_stable(page: Page, collector: EventCollector,
                       max_rounds: int = 60, pause_ms: int = 900) -> None:
    """Rola a pagina ate nao chegarem mais eventos novos por varias rodadas."""
    sem_novos = 0
    ultimo = collector.count()
    print(f"[scroll] iniciando com {ultimo} eventos coletados")
    for i in range(max_rounds):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(pause_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except PWTimeoutError:
            pass
        atual = collector.count()
        if atual == ultimo:
            sem_novos += 1
        else:
            sem_novos = 0
            print(f"[scroll] +{atual - ultimo} (total: {atual})")
            ultimo = atual
        if sem_novos >= 4:
            print(f"[scroll] estabilizou em {atual} eventos apos {i + 1} rolagens")
            break
    else:
        print(f"[scroll] max_rounds atingido com {collector.count()} eventos")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def scrape() -> None:
    collector = EventCollector()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 900},
            locale="pt-BR",
        )
        page = context.new_page()
        page.set_default_timeout(30000)
        page.on("response", collector.handle_response)

        print(f"[listing] abrindo {LISTING_URL}")
        page.goto(LISTING_URL, wait_until="domcontentloaded")
        _wait_listing_ready(page)
        page.wait_for_timeout(2000)

        scroll_until_stable(page, collector)

        # Garante mais alguns segundos para responses tardias.
        page.wait_for_timeout(2000)

        browser.close()

    eventos = [normalize_event(src) for src in collector.raw_sources]

    # Ordena: proximos primeiros; passados depois.
    def _sort_key(ev: dict[str, Any]) -> tuple:
        ts_list = ev.get("datas_timestamps_ms") or []
        agora = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        futuros = [t for t in ts_list if isinstance(t, (int, float)) and t >= agora]
        if futuros:
            return (0, min(futuros))
        if ts_list:
            return (1, -max(t for t in ts_list if isinstance(t, (int, float))))
        return (2, 0)

    eventos.sort(key=_sort_key)

    # Salva a primeira versao (sem datas_detalhadas) antes do enrichment
    # para nao perder nada se o enrichment falhar.
    OUTPUT_FILE.write_text(
        json.dumps(eventos, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[base] salvos {len(eventos)} eventos em {OUTPUT_FILE}")

    # Etapa 2: enrichment (pode ser desativado com SKIP_ENRICH=1)
    if os.environ.get("SKIP_ENRICH") == "1":
        print("[enrich] pulado (SKIP_ENRICH=1)")
    else:
        limit = int(os.environ.get("LIMIT", "0") or 0)
        alvos = eventos[:limit] if limit > 0 else eventos
        if limit > 0:
            print(f"[enrich] LIMIT={limit}: enriquecendo apenas {len(alvos)} eventos")
        enrich_events(alvos)

        OUTPUT_FILE.write_text(
            json.dumps(eventos, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n[final] salvos {len(eventos)} eventos em {OUTPUT_FILE}")


if __name__ == "__main__":
    try:
        scrape()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
