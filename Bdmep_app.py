#!/usr/bin/env python3
"""
BDMEP Web App — download de dados climáticos sem burocracia de email.

Instalação (em venv com playwright):
    pip install flask requests playwright
    playwright install chromium

Uso:
    python Bdmep_app.py
    Abra: http://localhost:5000
"""

# Auto-instala dependências se não encontradas
import subprocess, sys, os

def _ensure(pkg):
    try:
        __import__(pkg)
    except ImportError:
        print(f"Instalando {pkg}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg,
                               "--quiet", "--break-system-packages"],
                              stderr=subprocess.DEVNULL)

_ensure("flask")
_ensure("requests")


def _ensure_playwright():
    """Instala playwright e baixa o Chromium (uma vez só)."""
    try:
        import playwright  # noqa
    except ImportError:
        print("Instalando playwright...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "playwright",
             "--quiet", "--break-system-packages"],
            stderr=subprocess.DEVNULL,
        )
        print("playwright instalado.")

    marker = os.path.join(os.path.expanduser("~"), ".bdmep_chromium_ok")
    if not os.path.exists(marker):
        print("Baixando Chromium para automação (só na primeira vez, ~150 MB)...")
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0:
            try:
                open(marker, "w").close()
            except Exception:
                pass
            print("Chromium pronto.")
        else:
            print(f"Aviso: {result.stderr[:300]}")


_ensure_playwright()

import io
import json
import os
import threading
import time
import uuid
import zipfile
from datetime import datetime

from flask import Flask, jsonify, render_template_string, request, send_file, Response

try:
    import requests as req
except ImportError:
    raise SystemExit("Instale: pip install flask requests")

# ── API BDMEP ────────────────────────────────────────────────────────────────
API_BASE  = "https://apibdmep.inmet.gov.br"
API_TEMPO = "https://apitempo.inmet.gov.br"
FRONTEND  = "https://bdmep.inmet.gov.br"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Origin": FRONTEND,
    "Referer": f"{FRONTEND}/",
}

# Estação de referência para listar variáveis por tipo de estação
ESTACAO_REF = {
    "M": "83377",  # Brasília convencional
    "T": "A001",   # Brasília automática
}

# Estado das requisições em andamento (hash → dict com progresso)
jobs: dict = {}


def bdmep_session():
    s = req.Session()
    s.headers.update(HEADERS)
    try:
        s.get(FRONTEND, timeout=10)
    except Exception:
        pass
    return s


# ── FLASK APP ────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/estacoes")
def api_estacoes():
    tipo   = request.args.get("tipo", "M")
    regiao = request.args.get("regiao", "")
    regioes = [regiao] if regiao else ["N", "NO", "SU", "S", "CO"]
    s = bdmep_session()
    resultado = []
    for reg in regioes:
        r = s.get(f"{API_BASE}/{tipo}/R/{reg}", timeout=15)
        if r.status_code == 200 and r.text.startswith("["):
            resultado.extend(json.loads(r.text))
    return jsonify(sorted(resultado, key=lambda x: (x.get("SG_ESTADO",""), x.get("DC_NOME",""))))


@app.route("/api/variaveis")
def api_variaveis():
    tipo_dados   = request.args.get("tipo", "D")       # D, H, M
    tipo_estacao = request.args.get("tipo_estacao", "M")  # M ou T
    estacao_ref  = ESTACAO_REF.get(tipo_estacao, "83377")
    s = bdmep_session()
    r = s.get(f"{API_TEMPO}/BNDMET/atributos/{estacao_ref}/{tipo_dados}", timeout=15)
    if r.status_code == 200 and r.text.startswith("["):
        return jsonify(json.loads(r.text))
    return jsonify([])


@app.route("/api/requisicoes")
def api_requisicoes():
    """Lista requisições pendentes (não confirmadas) para o email."""
    email = request.args.get("email", "")
    if not email:
        return jsonify([])
    s = bdmep_session()
    r = s.post(f"{API_BASE}/requisicao/count", data={"email": email}, timeout=15)
    if r.status_code == 200 and r.text.startswith("["):
        return jsonify(json.loads(r.text))
    return jsonify([])


def _baixar_zip(s, hash_req: str, log_fn) -> bytes | None:
    """
    Tenta múltiplas variações de URL para baixar o ZIP.
    Detecta ZIP pelos magic bytes 'PK' caso o Content-Type seja incorreto.
    """
    import urllib.parse

    # Extrai parte do hash sem o prefixo bcrypt $2a$10$
    hash_curto = hash_req
    if hash_req.startswith("$2a$10$"):
        hash_curto = hash_req[7:]  # remove '$2a$10$'

    hash_encoded = urllib.parse.quote(hash_req, safe="")

    candidatos = [
        f"{FRONTEND}/{hash_curto}.zip",
        f"{FRONTEND}/{hash_encoded}.zip",
        f"{API_BASE}/requisicao/download/{hash_curto}",
        f"{API_BASE}/requisicao/download/{hash_encoded}",
        f"{FRONTEND}/{hash_req}.zip",
        f"{API_BASE}/download/{hash_curto}.zip",
    ]

    for url in candidatos:
        try:
            log_fn(f"Tentando URL: {url[:80]}...")
            r = s.get(url, timeout=60)
            content = r.content
            # Detecta ZIP pelos magic bytes PK
            if content[:2] == b"PK":
                log_fn(f"✓ ZIP encontrado em: {url[:80]}")
                return content
        except Exception as e:
            log_fn(f"  Falha: {e}")

    return None


def _submeter_via_browser(email: str, estacoes: list, variaveis: list,
                           tipo_dados: str, tipo_estacao: str, tipo_pontuacao: str,
                           data_inicio: str, data_fim: str, log_fn) -> dict:
    """
    Abre Chromium headless e navega pelo formulário real do BDMEP passo a passo.
    Intercepta a resposta de rede do POST /requisicao para capturar o hash diretamente.
    """
    from playwright.sync_api import sync_playwright

    def fmt_date(d):          # yyyy-mm-dd → dd/mm/yyyy
        y, m, day = d.split("-")
        return f"{day}/{m}/{y}"

    inicio_fmt = fmt_date(data_inicio)
    fim_fmt    = fmt_date(data_fim)

    hash_capturado = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-infobars",
            ]
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            }
        )
        page = context.new_page()

        # Esconde indicadores de automação
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
        """)

        # ── Intercepta resposta do POST /requisicao para capturar o hash ────
        def on_response(response):
            try:
                if "requisicao" in response.url and response.request.method == "POST":
                    try:
                        body = response.json()
                        if isinstance(body, list):
                            for item in body:
                                if isinstance(item, dict) and "hash" in item:
                                    hash_capturado.append(item["hash"])
                                    log_fn(f"✓ Hash interceptado: {item['hash'][:30]}...")
                        elif isinstance(body, dict) and "hash" in body:
                            hash_capturado.append(body["hash"])
                            log_fn(f"✓ Hash interceptado: {body['hash'][:30]}...")
                    except Exception:
                        pass
            except Exception:
                pass

        page.on("response", on_response)

        # ── Carrega o site (obtém cookies anti-bot F5/BIG-IP) ────────────────
        log_fn("Abrindo navegador e carregando BDMEP...")
        page.goto(FRONTEND, wait_until="networkidle", timeout=60_000)
        page.wait_for_timeout(3_000)

        # ── Passo 0 → 1: instruções iniciais ────────────────────────────────
        log_fn("Avançando instruções iniciais...")
        page.evaluate("""() => {
            const a = document.querySelector('a.instrucoes_proximo');
            if (a) a.click();
        }""")
        page.wait_for_timeout(1_500)

        # ── Passo 1: preencher email e avançar ──────────────────────────────
        log_fn("Preenchendo email no formulário...")
        page.evaluate(
            """(emailVal) => {
                const el = document.querySelector('input.email');
                if (el) {
                    el.value = emailVal;
                    el.dispatchEvent(new Event('input',  {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }""",
            email
        )
        page.wait_for_timeout(500)
        page.evaluate("""() => {
            const a = document.querySelector('a.form1_proximo');
            if (a) a.click();
        }""")
        page.wait_for_timeout(1_500)

        # ── Passo 2: configurar parâmetros ───────────────────────────────────
        log_fn(f"Selecionando tipo_dados={tipo_dados}, tipo_estacao={tipo_estacao}, "
               f"tipo_pontuacao={tipo_pontuacao}...")

        page.evaluate(
            """(params) => {
                const click = (name, val) => {
                    const el = document.querySelector(
                        'input[name="' + name + '"][value="' + val + '"]'
                    );
                    if (el) {
                        el.checked = true;
                        el.click();
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                };
                click('tipo_pontuacao', params.tipo_pontuacao);
                click('tipo_dados',     params.tipo_dados);
                click('tipo_estacao',   params.tipo_estacao);
                click('abrangencia',    'P');
            }""",
            {
                "tipo_pontuacao": tipo_pontuacao,
                "tipo_dados":     tipo_dados,
                "tipo_estacao":   tipo_estacao,
            }
        )
        page.wait_for_timeout(1_500)  # aguarda o formulário reagir à troca de tipo_estacao

        # Datas
        log_fn(f"Configurando período: {data_inicio} → {data_fim}...")
        page.evaluate(
            """(datas) => {
                const setDate = (sel, val) => {
                    const el = document.querySelector(sel);
                    if (!el) return;
                    el.value = val;
                    el.dispatchEvent(new Event('input',  {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('blur',   {bubbles: true}));
                };
                setDate('#datepickerInicio', datas.inicio);
                setDate('#datepickerFim',    datas.fim);
            }""",
            {"inicio": inicio_fmt, "fim": fim_fmt}
        )
        page.wait_for_timeout(500)

        # Variáveis: checkboxes CSS-hidden — set via JS
        log_fn(f"Selecionando {len(variaveis)} variável(is)...")
        nao_encontradas_var = page.evaluate(
            """(codes) => {
                const faltando = [];
                codes.forEach(code => {
                    const cb = document.querySelector(
                        'input[name="variaveis"][value="' + code + '"]'
                    );
                    if (cb) {
                        cb.checked = true;
                        cb.dispatchEvent(new Event('change', {bubbles: true}));
                    } else {
                        faltando.push(code);
                    }
                });
                return faltando;
            }""",
            variaveis
        )
        if nao_encontradas_var:
            log_fn(f"⚠ Variáveis não encontradas no formulário: {nao_encontradas_var}")
        page.wait_for_timeout(500)

        # Estações: checkboxes CSS-hidden — set via JS
        log_fn(f"Selecionando {len(estacoes)} estação(ões)...")
        nao_encontradas_est = page.evaluate(
            """(codes) => {
                const faltando = [];
                codes.forEach(code => {
                    const cb = document.querySelector(
                        'input[name="estacoes"][value="' + code + '"]'
                    );
                    if (cb) {
                        cb.checked = true;
                        cb.dispatchEvent(new Event('change', {bubbles: true}));
                    } else {
                        faltando.push(code);
                    }
                });
                return faltando;
            }""",
            estacoes
        )
        if nao_encontradas_est:
            log_fn(f"⚠ Estações não encontradas no formulário: {nao_encontradas_est}")
        page.wait_for_timeout(800)

        # ── Avançar para a tela de confirmação ──────────────────────────────
        log_fn("Avançando para confirmação...")
        page.evaluate("""() => {
            const a = document.querySelector('a.form2_proximo');
            if (a) a.click();
        }""")
        page.wait_for_timeout(2_500)

        try:
            page.screenshot(path="/tmp/bdmep_confirmacao.png")
        except Exception:
            pass

        # ── Confirmar — dispara o POST /requisicao real no servidor ─────────
        log_fn("Enviando confirmação ao servidor do BDMEP...")
        page.evaluate("""() => {
            const a = document.querySelector('a.confirmacao_confirmar');
            if (a) a.click();
        }""")
        page.wait_for_timeout(6_000)   # aguarda a requisição de rede completar

        try:
            page.screenshot(path="/tmp/bdmep_pos_confirmacao.png")
            titulo = page.title()
            log_fn(f"Página após confirmação: {titulo}")
        except Exception:
            pass

        # Verifica se algo está visível como erro no DOM
        try:
            msg_erro = page.evaluate("""() => {
                const el = document.querySelector('.alert-danger, .error, #erro, .mensagem-erro');
                return el ? el.innerText.trim() : null;
            }""")
            if msg_erro:
                log_fn(f"⚠ Mensagem do site: {msg_erro[:120]}")
        except Exception:
            pass

        browser.close()

    return {
        "status": 200,
        "text": "Formulário submetido via automação de navegador",
        "hash": hash_capturado[0] if hash_capturado else None,
    }


def _aguardar_e_baixar(s, hash_req: str, job: dict, log):
    """Confirma via GET no status endpoint, aguarda processamento e baixa o ZIP."""

    # Confirma (bypass do email)
    log("Confirmando automaticamente (sem email)...", "confirmando")
    s.get(f"{API_BASE}/requisicao/status/{hash_req}", timeout=15)
    time.sleep(2)

    # Aguarda processamento
    log("Aguardando processamento...", "processando")
    labels = {"1": "Na fila...", "2": "Processando dados...", "3": "Concluído!"}
    inicio = time.time()
    ultimo_status = None
    while time.time() - inicio < 600:
        r_st = s.get(f"{API_BASE}/requisicao/status/{hash_req}", timeout=15)
        text = r_st.text.strip()
        st   = "1" if text.startswith('["') else json.loads(text).get("status", "erro")
        if st != ultimo_status:
            log(labels.get(st, st))
            ultimo_status = st
        if st == "3":
            break
        time.sleep(8)
    else:
        job["fase"] = "erro"
        job["erro"] = "Timeout ao aguardar processamento (10 min)."
        return False

    # Baixar
    log("Baixando arquivo...", "baixando")
    zip_bytes = _baixar_zip(s, hash_req, log)
    if not zip_bytes:
        job["fase"] = "erro"
        job["erro"] = (
            "Arquivo ZIP não encontrado após múltiplas tentativas de URL.\n"
            "O processamento foi concluído pelo INMET, mas não foi possível baixar.\n"
            "Tente acessar bdmep.inmet.gov.br para baixar manualmente."
        )
        return False

    job["zip_bytes"] = zip_bytes
    return True


def _processar_job(job_id: str, email: str, estacoes: list, variaveis: list,
                   tipo_dados: str, tipo_estacao: str, tipo_pontuacao: str,
                   data_inicio: str, data_fim: str):
    """Worker: submete via browser → confirma → aguarda → baixa. Roda em thread."""
    job = jobs[job_id]
    s   = bdmep_session()

    def log(msg: str, fase: str = None):
        ts = datetime.now().strftime("%H:%M:%S")
        job["log"].append(f"[{ts}] {msg}")
        if fase:
            job["fase"] = fase

    try:
        # 1. Snapshot de hashes existentes (fallback caso a interceptação falhe)
        log("Verificando requisições existentes...", "submetendo")
        antes_raw = s.post(f"{API_BASE}/requisicao/count",
                           data={"email": email}, timeout=15).text
        antes = {e["hash"] for e in (json.loads(antes_raw)
                                     if antes_raw.startswith("[") else [])}
        log(f"Requisições existentes: {len(antes)}")

        # 2. Submeter via Playwright
        log("Iniciando navegador headless (leva ~20–30 s)...", "submetendo")
        try:
            resp = _submeter_via_browser(
                email, estacoes, variaveis,
                tipo_dados, tipo_estacao, tipo_pontuacao,
                data_inicio, data_fim, log,
            )
        except Exception as e:
            job["fase"] = "erro"
            job["erro"] = f"Erro ao abrir navegador: {e}"
            return

        log(f"Automação concluída — {resp.get('text','')}")

        # 3. Obter o hash — preferencialmente via interceptação, senão via /count
        hash_req = resp.get("hash")

        if hash_req:
            log(f"✓ Requisição criada: {hash_req[:30]}...", "confirmando")
            job["hash"] = hash_req
        else:
            # Fallback: detecta novo hash via diff de /count
            log("Hash não interceptado, verificando via API...", "confirmando")
            time.sleep(6)  # aguarda o servidor processar
            depois_raw = s.post(f"{API_BASE}/requisicao/count",
                                data={"email": email}, timeout=15).text
            depois = ({e["hash"]: e for e in json.loads(depois_raw)}
                      if depois_raw.startswith("[") else {})
            novos = set(depois.keys()) - antes

            if not novos:
                job["fase"] = "erro"
                job["erro"] = (
                    "O servidor não criou a requisição mesmo com automação de navegador.\n\n"
                    "Possíveis causas:\n"
                    "• O email informado não está cadastrado no BDMEP\n"
                    "• As estações ou variáveis selecionadas não estão disponíveis\n"
                    "• O site atualizou a estrutura do formulário\n"
                    "• O site bloqueou o acesso headless\n\n"
                    "Alternativa: submeta pelo site do BDMEP e use 'Confirmar Pendentes'."
                )
                return

            hash_req = list(novos)[0]
            job["hash"] = hash_req
            log(f"✓ Requisição detectada: {hash_req[:30]}...")

        # 4. Aguardar e baixar
        ok = _aguardar_e_baixar(s, hash_req, job, log)
        if not ok:
            return

        zip_bytes = job["zip_bytes"]
        job["zip_nome"] = f"bdmep_{tipo_dados}_{data_inicio}_{data_fim}.zip"

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            csvs = [n for n in zf.namelist() if n.endswith(".csv")]
        log(f"✓ Pronto! {len(csvs)} arquivo(s) CSV disponível(is) para download.")
        job["fase"]  = "pronto"
        job["csvs"]  = csvs

    except Exception as e:
        job["fase"] = "erro"
        job["erro"] = str(e)
        job["log"].append(f"[ERRO] {e}")


@app.route("/api/submeter", methods=["POST"])
def api_submeter():
    body = request.json or {}
    email          = body.get("email", "")
    estacoes       = body.get("estacoes", [])
    variaveis      = body.get("variaveis", [])
    tipo_dados     = body.get("tipo_dados", "D")
    tipo_estacao   = body.get("tipo_estacao", "M")
    tipo_pontuacao = body.get("tipo_pontuacao", "P")
    data_inicio    = body.get("data_inicio", "2000-01-01")
    data_fim       = body.get("data_fim", "2025-12-31")

    if not email or not estacoes or not variaveis:
        return jsonify({"erro": "email, estacoes e variaveis são obrigatórios"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"fase": "iniciando", "log": [], "hash": None,
                    "zip_bytes": None, "zip_nome": None, "csvs": [], "erro": None}

    t = threading.Thread(target=_processar_job, args=(
        job_id, email, estacoes, variaveis,
        tipo_dados, tipo_estacao, tipo_pontuacao, data_inicio, data_fim
    ), daemon=True)
    t.start()

    return jsonify({"job_id": job_id})


@app.route("/api/confirmar-pendentes", methods=["POST"])
def api_confirmar_pendentes():
    """Confirma todas as requisições pendentes de um email."""
    body  = request.json or {}
    email = body.get("email", "")
    if not email:
        return jsonify({"erro": "email obrigatório"}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {"fase": "iniciando", "log": [], "hash": None,
                    "zip_bytes": None, "zip_nome": None, "csvs": [], "erro": None}

    def _confirmar():
        job = jobs[job_id]
        s   = bdmep_session()

        def log(msg, fase=None):
            ts = datetime.now().strftime("%H:%M:%S")
            job["log"].append(f"[{ts}] {msg}")
            if fase:
                job["fase"] = fase

        try:
            pendentes_raw = s.post(f"{API_BASE}/requisicao/count",
                                   data={"email": email}, timeout=15).text
            pendentes = json.loads(pendentes_raw) if pendentes_raw.startswith("[") else []

            if not pendentes:
                log("Nenhuma requisição pendente encontrada.")
                job["fase"] = "erro"
                job["erro"] = "Nenhuma requisição pendente. Submeta pelo site do BDMEP primeiro."
                return

            log(f"{len(pendentes)} requisição(ões) pendente(s) encontrada(s).", "confirmando")

            for p in pendentes:
                h = p["hash"]
                log(f"Confirmando {h[:30]}...")
                s.get(f"{API_BASE}/requisicao/status/{h}", timeout=15)
                time.sleep(1)

            hash_req = pendentes[-1]["hash"]
            job["hash"] = hash_req

            ok = _aguardar_e_baixar(s, hash_req, job, log)
            if not ok:
                return

            zip_bytes = job["zip_bytes"]
            job["zip_nome"] = f"bdmep_{email.split('@')[0]}.zip"

            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                csvs = [n for n in zf.namelist() if n.endswith(".csv")]
            log(f"✓ Pronto! {len(csvs)} arquivo(s) CSV disponíveis.")
            job["fase"] = "pronto"
            job["csvs"] = csvs

        except Exception as e:
            job["fase"] = "erro"
            job["erro"] = str(e)

    threading.Thread(target=_confirmar, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/progresso/<job_id>")
def api_progresso(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"erro": "job não encontrado"}), 404
    return jsonify({
        "fase":  job["fase"],
        "log":   job["log"],
        "csvs":  job["csvs"],
        "erro":  job["erro"],
        "pronto": job["fase"] == "pronto",
    })


@app.route("/api/download/<job_id>")
def api_download(job_id):
    job = jobs.get(job_id)
    if not job or not job.get("zip_bytes"):
        return "Arquivo não disponível", 404
    return send_file(
        io.BytesIO(job["zip_bytes"]),
        mimetype="application/zip",
        as_attachment=True,
        download_name=job.get("zip_nome", "bdmep.zip"),
    )


# ── HTML / FRONTEND ──────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BDMEP Downloader</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, -apple-system, sans-serif; background: #f0f4f8; color: #1a202c; }

  header {
    background: linear-gradient(135deg, #1a4a7a 0%, #2563eb 100%);
    color: white; padding: 20px 32px; display: flex; align-items: center; gap: 16px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.2);
  }
  header h1 { font-size: 1.4rem; font-weight: 700; }
  header p  { font-size: 0.85rem; opacity: 0.85; margin-top: 2px; }
  .logo { font-size: 2rem; }

  .container { max-width: 960px; margin: 0 auto; padding: 28px 20px; }

  .card {
    background: white; border-radius: 12px; padding: 24px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.08); margin-bottom: 20px;
  }
  .card h2 { font-size: 1rem; font-weight: 600; color: #374151; margin-bottom: 16px;
    border-bottom: 2px solid #e5e7eb; padding-bottom: 10px; }

  label { display: block; font-size: 0.85rem; font-weight: 500; color: #374151; margin-bottom: 5px; }
  input[type=text], input[type=email], input[type=date], select {
    width: 100%; padding: 9px 12px; border: 1px solid #d1d5db; border-radius: 8px;
    font-size: 0.9rem; background: #f9fafb; transition: border-color .2s;
  }
  input:focus, select:focus { outline: none; border-color: #2563eb; background: white; }

  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }

  .radio-group { display: flex; gap: 8px; flex-wrap: wrap; }
  .radio-btn input { display: none; }
  .radio-btn label {
    display: inline-flex; align-items: center; padding: 7px 14px;
    border: 2px solid #e5e7eb; border-radius: 8px; cursor: pointer;
    font-size: 0.85rem; font-weight: 500; transition: all .15s; margin: 0;
    background: white;
  }
  .radio-btn input:checked + label {
    border-color: #2563eb; background: #eff6ff; color: #1d4ed8;
  }

  .searchbox { position: relative; margin-bottom: 8px; }
  .searchbox input { padding-left: 36px; background: white; }
  .searchbox::before { content: "🔍"; position: absolute; left: 10px; top: 50%; transform: translateY(-50%);
    font-size: 0.9rem; pointer-events: none; }

  .list-box {
    border: 1px solid #d1d5db; border-radius: 8px; height: 220px; overflow-y: auto;
    background: #f9fafb;
  }
  .list-box label {
    display: flex; align-items: flex-start; gap: 10px; padding: 8px 12px;
    cursor: pointer; font-weight: 400; font-size: 0.82rem; color: #374151;
    border-bottom: 1px solid #f3f4f6; transition: background .1s; margin: 0;
  }
  .list-box label:last-child { border-bottom: none; }
  .list-box label:hover { background: #eff6ff; }
  .list-box input[type=checkbox] { margin-top: 2px; accent-color: #2563eb; flex-shrink: 0; }
  .list-box .badge {
    margin-left: auto; font-size: 0.72rem; color: #6b7280;
    background: #f3f4f6; padding: 1px 6px; border-radius: 4px; white-space: nowrap;
  }
  .inoperante { opacity: 0.45; }

  .sel-count { font-size: 0.8rem; color: #6b7280; margin-bottom: 6px; }

  .btn {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 10px 20px; border-radius: 8px; font-size: 0.9rem; font-weight: 600;
    border: none; cursor: pointer; transition: all .15s;
  }
  .btn-primary { background: #2563eb; color: white; }
  .btn-primary:hover { background: #1d4ed8; transform: translateY(-1px); }
  .btn-primary:disabled { background: #93c5fd; cursor: not-allowed; transform: none; }
  .btn-secondary { background: #f3f4f6; color: #374151; border: 1px solid #d1d5db; }
  .btn-secondary:hover { background: #e5e7eb; }
  .btn-success { background: #16a34a; color: white; }
  .btn-success:hover { background: #15803d; }
  .btn-warning { background: #d97706; color: white; font-size: 0.85rem; padding: 8px 16px; }
  .btn-warning:hover { background: #b45309; }

  .actions { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-top: 4px; }

  /* Progress */
  #progress-card { display: none; }
  .progress-bar-wrap { height: 8px; background: #e5e7eb; border-radius: 99px; margin: 12px 0; }
  .progress-bar { height: 100%; background: linear-gradient(90deg, #2563eb, #7c3aed);
    border-radius: 99px; width: 0%; transition: width .5s; animation: pulse-bar 2s infinite; }
  @keyframes pulse-bar { 0%,100%{opacity:1} 50%{opacity:.7} }
  .progress-bar.done { animation: none; background: #16a34a; width: 100%; }
  .progress-bar.error { animation: none; background: #dc2626; }

  .log-box {
    background: #1e293b; color: #94a3b8; border-radius: 8px; padding: 14px 16px;
    font-family: monospace; font-size: 0.8rem; max-height: 180px; overflow-y: auto;
    line-height: 1.6;
  }
  .log-box .ok { color: #4ade80; }
  .log-box .warn { color: #facc15; }
  .log-box .err { color: #f87171; }

  .fase-badge {
    display: inline-block; padding: 3px 10px; border-radius: 99px;
    font-size: 0.78rem; font-weight: 600; text-transform: uppercase; letter-spacing: .04em;
  }
  .fase-iniciando, .fase-submetendo, .fase-confirmando { background: #dbeafe; color: #1d4ed8; }
  .fase-processando, .fase-aguardando { background: #fef3c7; color: #92400e; }
  .fase-baixando  { background: #e0e7ff; color: #4338ca; }
  .fase-pronto    { background: #dcfce7; color: #166534; }
  .fase-erro      { background: #fee2e2; color: #991b1b; }

  .alert { padding: 12px 16px; border-radius: 8px; font-size: 0.85rem; margin-bottom: 16px; }
  .alert-info { background: #eff6ff; border: 1px solid #bfdbfe; color: #1d4ed8; }
  .alert-warn { background: #fffbeb; border: 1px solid #fde68a; color: #92400e; }

  @media (max-width: 600px) { .grid-2, .grid-3 { grid-template-columns: 1fr; } }
</style>
</head>
<body>

<header>
  <div class="logo">🌦️</div>
  <div>
    <h1>BDMEP Downloader</h1>
    <p>Dados climáticos do INMET — sem esperar emails</p>
  </div>
</header>

<div class="container">

  <div class="alert alert-warn">
    ⚠️ <strong>Ferramenta não oficial.</strong> Este projeto não tem vínculo com o INMET.
    Os dados são fornecidos pelo INMET via BDMEP. Não me responsabilizo pela integridade
    ou disponibilidade dos dados obtidos por esta ferramenta.
  </div>

  <div class="alert alert-info">
    <strong>Como funciona:</strong> Preencha o formulário e clique <strong>Baixar Dados</strong>.
    O app abre um navegador em segundo plano, clica em cada passo do formulário BDMEP,
    confirma e baixa automaticamente — sem emails, sem cliques extras.
    A submissão leva ~20–30 segundos (o navegador precisa carregar o site).
  </div>

  <!-- Email -->
  <div class="card">
    <h2>📧 Email</h2>
    <label for="email">Email cadastrado no BDMEP</label>
    <input type="email" id="email" placeholder="voce@email.com" autocomplete="email">
  </div>

  <!-- Configurações -->
  <div class="card">
    <h2>⚙️ Configurações</h2>
    <div class="grid-3">
      <div>
        <label>Tipo de Dados</label>
        <div class="radio-group" id="tipo-dados">
          <div class="radio-btn"><input type="radio" name="tipo_dados" id="td-D" value="D" checked><label for="td-D">Diários</label></div>
          <div class="radio-btn"><input type="radio" name="tipo_dados" id="td-H" value="H"><label for="td-H">Horários</label></div>
          <div class="radio-btn"><input type="radio" name="tipo_dados" id="td-M" value="M"><label for="td-M">Mensais</label></div>
        </div>
      </div>
      <div>
        <label>Tipo de Estação</label>
        <div class="radio-group" id="tipo-estacao">
          <div class="radio-btn"><input type="radio" name="tipo_estacao" id="te-M" value="M" checked><label for="te-M">Convencionais</label></div>
          <div class="radio-btn"><input type="radio" name="tipo_estacao" id="te-T" value="T"><label for="te-T">Automáticas</label></div>
        </div>
      </div>
      <div>
        <label>Separador Decimal</label>
        <div class="radio-group">
          <div class="radio-btn"><input type="radio" name="tipo_pontuacao" id="tp-P" value="P" checked><label for="tp-P">Ponto ( . )</label></div>
          <div class="radio-btn"><input type="radio" name="tipo_pontuacao" id="tp-V" value="V"><label for="tp-V">Vírgula ( , )</label></div>
        </div>
      </div>
    </div>

    <div class="grid-2" style="margin-top:16px">
      <div>
        <label for="data-inicio">Data Início</label>
        <input type="date" id="data-inicio" value="2000-01-01">
      </div>
      <div>
        <label for="data-fim">Data Fim</label>
        <input type="date" id="data-fim">
      </div>
    </div>
  </div>

  <!-- Estações -->
  <div class="card">
    <h2>📍 Estações</h2>
    <div class="grid-2" style="margin-bottom:12px">
      <div>
        <label>Filtrar por Região</label>
        <div class="radio-group" id="regiao-filter">
          <div class="radio-btn"><input type="radio" name="regiao" id="reg-all" value="" checked><label for="reg-all">Todas</label></div>
          <div class="radio-btn"><input type="radio" name="regiao" id="reg-N"  value="N"><label for="reg-N">Norte</label></div>
          <div class="radio-btn"><input type="radio" name="regiao" id="reg-NO" value="NO"><label for="reg-NO">Nordeste</label></div>
          <div class="radio-btn"><input type="radio" name="regiao" id="reg-CO" value="CO"><label for="reg-CO">Centro-Oeste</label></div>
          <div class="radio-btn"><input type="radio" name="regiao" id="reg-SU" value="SU"><label for="reg-SU">Sudeste</label></div>
          <div class="radio-btn"><input type="radio" name="regiao" id="reg-S"  value="S"><label for="reg-S">Sul</label></div>
        </div>
      </div>
      <div>
        <label>&nbsp;</label>
        <div class="searchbox">
          <input type="text" id="busca-estacao" placeholder="Buscar por nome ou código...">
        </div>
      </div>
    </div>
    <div class="sel-count" id="est-count">Carregando estações...</div>
    <div class="list-box" id="lista-estacoes"><div style="padding:16px;color:#9ca3af;text-align:center">Carregando...</div></div>
  </div>

  <!-- Variáveis -->
  <div class="card">
    <h2>📊 Variáveis</h2>
    <div class="sel-count" id="var-count">–</div>
    <div class="list-box" id="lista-variaveis"><div style="padding:16px;color:#9ca3af;text-align:center">Selecione o tipo de dados primeiro</div></div>
    <div style="margin-top:10px; display:flex; gap:8px;">
      <button class="btn btn-secondary" onclick="selecionarTodos('lista-variaveis')">Selecionar todas</button>
      <button class="btn btn-secondary" onclick="deselecionarTodos('lista-variaveis')">Limpar</button>
    </div>
  </div>

  <!-- Ações -->
  <div class="card">
    <h2>🚀 Download</h2>
    <div class="actions">
      <button class="btn btn-primary" id="btn-baixar" onclick="iniciarDownload()">
        ⬇️ Baixar Dados
      </button>
      <span style="color:#9ca3af; font-size:.85rem">ou</span>
      <button class="btn btn-warning" onclick="confirmarPendentes()">
        📬 Confirmar Pendentes
      </button>
    </div>
    <p style="font-size:0.78rem; color:#9ca3af; margin-top:10px;">
      <strong>Baixar Dados</strong>: abre navegador, preenche o formulário e baixa automaticamente.<br>
      <strong>Confirmar Pendentes</strong>: se você já submeteu pelo site do BDMEP, use este botão para confirmar e baixar sem precisar clicar no email.
    </p>
  </div>

  <!-- Progresso -->
  <div class="card" id="progress-card">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
      <h2 style="margin:0; border:none; padding:0;">Progresso</h2>
      <span class="fase-badge" id="fase-badge">–</span>
    </div>
    <div class="progress-bar-wrap"><div class="progress-bar" id="progress-bar"></div></div>
    <div class="log-box" id="log-box"></div>
    <div id="download-area" style="display:none; margin-top:16px;">
      <div class="alert" style="background:#dcfce7; border:1px solid #86efac; color:#166534; margin-bottom:12px;">
        ✅ Download concluído! Os arquivos CSV estão prontos.
      </div>
      <a class="btn btn-success" id="btn-download-link" href="#" download>
        💾 Baixar ZIP com CSVs
      </a>
    </div>
    <div id="erro-area" style="display:none; margin-top:16px;">
      <div class="alert" style="background:#fee2e2; border:1px solid #fca5a5; color:#991b1b; white-space:pre-line;" id="erro-msg"></div>
    </div>
  </div>

</div>

<footer style="background:#1e293b; color:#94a3b8; text-align:center; padding:16px; font-size:0.8rem; margin-top:8px;">
  Desenvolvido por <strong style="color:#e2e8f0;">Rui Ogawa</strong> —
  ruiogawa@gmail.com —
  <a href="https://github.com/ruiogawa/bdmep-downloader" style="color:#60a5fa;" target="_blank">github.com/ruiogawa/bdmep-downloader</a>
</footer>

<script>
let currentJobId = null;
let pollInterval = null;
let estacoesDados = [];

document.addEventListener("DOMContentLoaded", () => {
  const hoje = new Date().toISOString().slice(0, 10);
  document.getElementById("data-fim").value = hoje;

  carregarEstacoes();
  carregarVariaveis();

  document.querySelectorAll("input[name=tipo_estacao]").forEach(r =>
    r.addEventListener("change", () => {
      deselecionarTodos("lista-estacoes");
      deselecionarTodos("lista-variaveis");
      carregarEstacoes();
      carregarVariaveis();   // recarrega variáveis ao mudar tipo de estação
    })
  );
  document.querySelectorAll("input[name=tipo_dados]").forEach(r =>
    r.addEventListener("change", () => { deselecionarTodos("lista-variaveis"); carregarVariaveis(); })
  );
  document.querySelectorAll("input[name=regiao]").forEach(r =>
    r.addEventListener("change", renderizarEstacoes)
  );
  document.getElementById("busca-estacao").addEventListener("input", renderizarEstacoes);
});

async function carregarEstacoes() {
  const tipo = document.querySelector("input[name=tipo_estacao]:checked").value;
  document.getElementById("lista-estacoes").innerHTML =
    '<div style="padding:16px;color:#9ca3af;text-align:center">Carregando...</div>';
  document.getElementById("est-count").textContent = "Carregando...";

  const resp = await fetch(`/api/estacoes?tipo=${tipo}`);
  estacoesDados = await resp.json();
  renderizarEstacoes();
}

function renderizarEstacoes() {
  const regiao = document.querySelector("input[name=regiao]:checked").value;
  const busca  = document.getElementById("busca-estacao").value.toLowerCase();

  const filtradas = estacoesDados.filter(e => {
    if (regiao && e.SG_REGIAO !== regiao) return false;
    if (busca && !e.DC_NOME.toLowerCase().includes(busca) &&
        !e.CD_ESTACAO.includes(busca) && !e.SG_ESTADO.toLowerCase().includes(busca)) return false;
    return true;
  });

  const lista = document.getElementById("lista-estacoes");
  if (!filtradas.length) {
    lista.innerHTML = '<div style="padding:16px;color:#9ca3af;text-align:center">Nenhuma estação encontrada</div>';
    document.getElementById("est-count").textContent = "0 estações";
    return;
  }

  lista.innerHTML = filtradas.map(e => {
    const operante = e.CD_SITUACAO === "Operante";
    return `<label class="${operante ? "" : "inoperante"}">
      <input type="checkbox" name="estacoes" value="${e.CD_ESTACAO}">
      <span>
        <strong>${e.CD_ESTACAO}</strong> — ${e.DC_NOME}
        ${!operante ? ' <em style="color:#ef4444; font-size:.75rem">(inoperante)</em>' : ""}
      </span>
      <span class="badge">${e.SG_ESTADO} · ${e.SG_REGIAO}</span>
    </label>`;
  }).join("");

  document.getElementById("est-count").textContent =
    `${filtradas.length} estações — clique para selecionar`;
}

async function carregarVariaveis() {
  const tipo_dados   = document.querySelector("input[name=tipo_dados]:checked").value;
  const tipo_estacao = document.querySelector("input[name=tipo_estacao]:checked").value;
  document.getElementById("lista-variaveis").innerHTML =
    '<div style="padding:16px;color:#9ca3af;text-align:center">Carregando...</div>';

  const resp = await fetch(`/api/variaveis?tipo=${tipo_dados}&tipo_estacao=${tipo_estacao}`);
  const vars = await resp.json();

  const lista = document.getElementById("lista-variaveis");
  if (!vars.length) {
    lista.innerHTML = '<div style="padding:16px;color:#9ca3af">Nenhuma variável encontrada</div>';
    return;
  }

  lista.innerHTML = vars.map(v =>
    `<label>
      <input type="checkbox" name="variaveis" value="${v.CODIGO}" checked>
      <span>${v.DESCRICAO.toLowerCase().replace(/^[a-z]/, c => c.toUpperCase())}</span>
      <span class="badge">${v.UNIDADE} · ${v.CLASSE}</span>
    </label>`
  ).join("");

  atualizarContVar();
  lista.querySelectorAll("input[type=checkbox]").forEach(cb =>
    cb.addEventListener("change", atualizarContVar)
  );
}

function atualizarContVar() {
  const total = document.querySelectorAll("#lista-variaveis input").length;
  const sel   = document.querySelectorAll("#lista-variaveis input:checked").length;
  document.getElementById("var-count").textContent = `${sel} de ${total} variáveis selecionadas`;
}

function selecionarTodos(listId) {
  document.querySelectorAll(`#${listId} input[type=checkbox]`).forEach(cb => cb.checked = true);
  if (listId === "lista-variaveis") atualizarContVar();
}
function deselecionarTodos(listId) {
  document.querySelectorAll(`#${listId} input[type=checkbox]`).forEach(cb => cb.checked = false);
  if (listId === "lista-variaveis") atualizarContVar();
}

function getSelecao(name) {
  return [...document.querySelectorAll(`input[name=${name}]:checked`)].map(el => el.value);
}

async function iniciarDownload() {
  const email     = document.getElementById("email").value.trim();
  const estacoes  = getSelecao("estacoes");
  const variaveis = getSelecao("variaveis");

  if (!email)           return alert("Informe seu email.");
  if (!estacoes.length)  return alert("Selecione ao menos uma estação.");
  if (!variaveis.length) return alert("Selecione ao menos uma variável.");

  const body = {
    email,
    estacoes,
    variaveis,
    tipo_dados:     document.querySelector("input[name=tipo_dados]:checked").value,
    tipo_estacao:   document.querySelector("input[name=tipo_estacao]:checked").value,
    tipo_pontuacao: document.querySelector("input[name=tipo_pontuacao]:checked").value,
    data_inicio:    document.getElementById("data-inicio").value,
    data_fim:       document.getElementById("data-fim").value,
  };

  const resp = await fetch("/api/submeter", {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
  });
  const { job_id, erro } = await resp.json();
  if (erro) return alert("Erro: " + erro);

  iniciarPolling(job_id);
}

async function confirmarPendentes() {
  const email = document.getElementById("email").value.trim();
  if (!email) return alert("Informe seu email.");

  const resp = await fetch("/api/confirmar-pendentes", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ email })
  });
  const { job_id, erro } = await resp.json();
  if (erro) return alert("Erro: " + erro);
  iniciarPolling(job_id);
}

const FASE_LABELS = {
  iniciando: "Iniciando", submetendo: "Submetendo", confirmando: "Confirmando",
  processando: "Processando", aguardando: "Aguardando", baixando: "Baixando",
  pronto: "Pronto!", erro: "Erro"
};
const FASE_PROGRESS = {
  iniciando: 5, submetendo: 20, confirmando: 35,
  processando: 55, aguardando: 55, baixando: 85, pronto: 100, erro: 100
};

function iniciarPolling(jobId) {
  currentJobId = jobId;
  if (pollInterval) clearInterval(pollInterval);

  const card = document.getElementById("progress-card");
  card.style.display = "block";
  card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  document.getElementById("download-area").style.display = "none";
  document.getElementById("erro-area").style.display    = "none";
  document.getElementById("log-box").innerHTML = "";
  document.getElementById("btn-baixar").disabled = true;

  pollInterval = setInterval(() => pollProgresso(jobId), 2000);
  pollProgresso(jobId);
}

async function pollProgresso(jobId) {
  const resp = await fetch(`/api/progresso/${jobId}`);
  const data = await resp.json();

  const badge = document.getElementById("fase-badge");
  badge.textContent  = FASE_LABELS[data.fase] || data.fase;
  badge.className    = `fase-badge fase-${data.fase}`;

  const bar = document.getElementById("progress-bar");
  bar.style.width = (FASE_PROGRESS[data.fase] || 5) + "%";
  bar.className   = "progress-bar" + (data.fase === "pronto" ? " done" : data.fase === "erro" ? " error" : "");

  const logBox = document.getElementById("log-box");
  logBox.innerHTML = data.log.map(line => {
    if (line.includes("✓") || line.includes("Pronto")) return `<div class="ok">${line}</div>`;
    if (line.includes("⚠") || line.includes("Aviso")) return `<div class="warn">${line}</div>`;
    if (line.includes("[ERRO]")) return `<div class="err">${line}</div>`;
    return `<div>${line}</div>`;
  }).join("");
  logBox.scrollTop = logBox.scrollHeight;

  if (data.pronto) {
    clearInterval(pollInterval);
    document.getElementById("btn-baixar").disabled = false;
    document.getElementById("download-area").style.display = "block";
    document.getElementById("btn-download-link").href = `/api/download/${jobId}`;
  }

  if (data.fase === "erro") {
    clearInterval(pollInterval);
    document.getElementById("btn-baixar").disabled = false;
    document.getElementById("erro-area").style.display = "block";
    document.getElementById("erro-msg").textContent = data.erro || "Erro desconhecido.";
  }
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    print("=" * 50)
    print(" BDMEP Downloader")
    print(" Abra no navegador: http://localhost:5000")
    print("=" * 50)
    app.run(debug=False, host="0.0.0.0", port=5000)
