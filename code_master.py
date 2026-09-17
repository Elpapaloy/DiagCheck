import re, json, subprocess, sys, tempfile, os, csv
import pandas as pd
from dotenv import load_dotenv
from pathlib import Path
from difflib import SequenceMatcher

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)


def _req(nombre: str) -> Path:
    v = os.getenv(nombre)
    if not v:
        sys.exit(f"ERROR: falta {nombre} en {env_path}")
    return Path(v)


# ES NECESARIO CREAR UN ARCHIVO .env CON LAS RUTAS A LOS DATOS
path            = _req("source_path")
output_path     = _req("output_path")
claude_bin      = _req("CLAUDE_BIN")
marcados        = _req("MARCADOS")
PROMPT_RECONOCIMIENTO = _req("PROMPT_RECONOCIMIENTO")
PROMPT_EXTRACCION     = _req("PROMPT_EXTRACCION")

if not claude_bin.is_file():
    sys.exit(f"ERROR: {claude_bin} no es un archivo (¿falta claude.exe?)")
        
PROMPT_VERSION = "reconocimiento_v1"
ETAPAS = ["reconocimiento_a", "reconocimiento_b", "reconciliacion", "final"]
MARCADORES_LIMITE = ["session limit", "hit your session limit"]
MARCADORES_AUTH = ["not logged in", "please run /login", "invalid api key",
                   "oauth token has expired", "authentication_error"]
METRICS_DIR = Path(__file__).resolve().parent / "metricas_promptciego"
METRICS_FILE     = METRICS_DIR / "metricas_nuevas.csv"
METRICS_LIT_FILE = METRICS_DIR / "metricas_literalidad.csv"



MODEL_RECONOCIMIENTO = "claude-haiku-4-5"
MODEL_EXTRACCION     = "claude-sonnet-5"


class LimiteAlcanzado(Exception):
    pass


class SinAutenticacion(Exception):
    pass


if not (os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY")):
    print("AVISO: no hay CLAUDE_CODE_OAUTH_TOKEN ni ANTHROPIC_API_KEY; "
          "se usará la sesión interactiva de claude, que puede caducar en corridas "
          "desatendidas (ver 'claude setup-token')")


# --------------------------------------------------------------------- #
# Infraestructura de llamada (única que toca subprocess)
# --------------------------------------------------------------------- #

def llamar_claude(prompt: str, model) -> dict:
    with tempfile.TemporaryDirectory() as tmp_cwd:
        proc = subprocess.run(
            [str(claude_bin), "-p", "--output-format", "json",
             "--permission-mode", "dontAsk",
             "--disallowedTools", "*",
             "--max-turns", "1",
             "--model", model],
            input=prompt,
            capture_output=True,
            encoding="utf-8",
            text=True,
            timeout=1500,
            cwd=tmp_cwd,
        )

        try:
            meta = json.loads(proc.stdout)
            if meta.get("permission_denials"):
                print(f"  (herramientas bloqueadas: {meta['permission_denials']})")
        except json.JSONDecodeError:
            crudo = (proc.stderr or proc.stdout).strip()
            if any(m in crudo.lower() for m in MARCADORES_AUTH):
                raise SinAutenticacion(crudo[:200])
            raise RuntimeError(
                f"salida no parseable (rc={proc.returncode}): {crudo[:300]}"
            )

        if meta.get("is_error"):
            mensaje = meta.get("result", "")
            if any(m in mensaje.lower() for m in MARCADORES_LIMITE):
                raise LimiteAlcanzado(mensaje[:200])
            if any(m in mensaje.lower() for m in MARCADORES_AUTH):
                raise SinAutenticacion(mensaje[:200])
            raise RuntimeError(f"claude reportó error: {mensaje[:300]}")

        return meta


def extract_json_block(raw_text: str) -> dict:
    text = raw_text.strip()

    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start == -1:
        raise ValueError("No se encontró '{' en la salida del modelo")

    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])

    raise ValueError("No se encontró un bloque JSON balanceado en la salida")


def escribir_salida(datos, destino: Path):
    tmp = destino.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(destino)


def registrar_metrica(archivo: str, etapa: str, meta: dict, duracion_s: float, model):
    u = meta.get("usage", {})
    existe = METRICS_FILE.exists()
    with open(METRICS_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not existe:
            w.writerow(["archivo", "etapa", "prompt_version", "modelo", "costo_usd",
                        "duracion_s", "input", "cache_creation", "cache_read", "output"])
        w.writerow([archivo, etapa, PROMPT_VERSION, model,
                    meta.get("total_cost_usd", 0), round(duracion_s, 1),
                    u.get("input_tokens", 0),
                    u.get("cache_creation_input_tokens", 0),
                    u.get("cache_read_input_tokens", 0),
                    u.get("output_tokens", 0)])


# --------------------------------------------------------------------- #
# Verificación de literalidad y reconciliación (sin costo de API)
# --------------------------------------------------------------------- #

def normalizar(texto: str) -> str:
    return re.sub(r"\s+", " ", texto.strip())


def mejor_coincidencia(literal_norm: str, cita_norm: str) -> dict:
    """Ambos argumentos YA vienen normalizados (evita re-normalizar por candidato)."""
    sm = SequenceMatcher(None, literal_norm, cita_norm, autojunk=False)
    match = sm.find_longest_match(0, len(literal_norm), 0, len(cita_norm))
    cobertura = match.size / max(1, len(literal_norm))
    return {
        "cobertura": round(cobertura, 3),
        "rango": (match.b, match.b + match.size) if match.size else None,
    }


def clasificar_match(cobertura: float) -> str:
    if cobertura >= 0.95:
        return "ok"
    elif cobertura >= 0.59:
        return "posible_parafraseo"
    return "no_encontrado"


def evaluar_candidato(candidato: dict, citas_norm: dict) -> dict:
    cita_txt = citas_norm.get(candidato.get("cita_origen"))
    if cita_txt is None:
        return {"estado": "cita_inexistente", "cobertura": 0.0, "rango": None}
    r = mejor_coincidencia(normalizar(candidato.get("texto_literal", "")), cita_txt)
    return {"estado": clasificar_match(r["cobertura"]), **r}


def solapan(r1, r2) -> bool: # como recibe el porcentaje de coincidencia y el rango
    return bool(r1 and r2 and r1[0] <= r2[1] and r2[0] <= r1[1])

def registrar_literalidad(paciente_id: str, corrida: str, candidatos: list):
    existe = METRICS_LIT_FILE.exists()
    with open(METRICS_LIT_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not existe:
            w.writerow(["paciente_id", "corrida","candidato_id", "dominio", "cita_origen",
                        "cobertura", "estado","duplicados_a_b"])
        for c in candidatos:
            w.writerow([paciente_id, corrida, c.get("candidato_id"), c.get("dominio"),
                        c.get("cita_origen"),
                        c["_eval"]["cobertura"], c["_eval"]["estado"], c.get("duplicados_a_b", "no hay")])

def deduplicar(candidatos: list) -> tuple[list, int]:
    vistos, unicos = set(), []
    for c in candidatos:
        clave = (c.get("cita_origen"), c.get("dominio"),
                 normalizar(c.get("texto_literal", "")))
        if clave in vistos:
            continue
        vistos.add(clave)
        unicos.append(c)
    return unicos, len(candidatos) - len(unicos)

def reconciliar_indicios(paciente_id, cand_a: list, cand_b: list, citas_norm: dict) -> dict:
    cand_a, n_dup_a = deduplicar(cand_a)
    cand_b, n_dup_b = deduplicar(cand_b)

    for corrida, lista, n_dup in (("a", cand_a, n_dup_a), ("b", cand_b, n_dup_b)):
        for i, c in enumerate(lista):
            c["_eval"] = evaluar_candidato(c, citas_norm)
            c["candidato_id"] = f"{paciente_id}:{corrida}:{c.get('cita_origen')}:{c.get('dominio')}:{i:03d}"
            c["duplicados_a_b"] = n_dup
    registrar_literalidad(paciente_id, "a",cand_a)
    registrar_literalidad(paciente_id, "b",cand_b)

    coincidencias, solo_a = [], []
    b_usados = set()
    for ca in cand_a: #
        idx = next( (i for i, cb in enumerate(cand_b)
            if i not in b_usados
            and ca.get("cita_origen") == cb.get("cita_origen")   # rangos son relativos a su cita
            and ca.get("dominio") == cb.get("dominio")
            and solapan(ca["_eval"]["rango"], cb["_eval"]["rango"])),
            None)
        if idx is not None:
            coincidencias.append((ca, cand_b[idx]))
            b_usados.add(idx)
        else:
            solo_a.append(ca)

    solo_b = [cb for i, cb in enumerate(cand_b) if i not in b_usados]
    return {"coincidencias": coincidencias, "solo_a": solo_a, "solo_b": solo_b,
            "n_a": len(cand_a), "n_b": len(cand_b),
            "n_dup_a": n_dup_a, "n_dup_b": n_dup_b}

def fusionar_para_revision(rec: dict) -> list:
    todos = [x for par in rec["coincidencias"] for x in par] + rec["solo_a"] + rec["solo_b"]
    n_desc = sum(1 for x in todos if not valido(x))

    salida = [{**a, "candidato_id_par": b["candidato_id"], "confirmado_por": 2, "confianza": "alta"}
              for a, b in rec["coincidencias"] if valido(a) and valido(b)]
    salida += [{**x, "confirmado_por": 1, "confianza": "revisar"}
               for x in rec["solo_a"] + rec["solo_b"] if valido(x)]
    return salida, n_desc

def valido(c: dict) -> bool:
    return c["_eval"]["estado"] in ("ok", "posible_parafraseo")

METRICS_REC_FILE = METRICS_DIR / "metricas_reconciliacion.csv"

def registrar_reconciliacion(paciente_id: str, rec: dict, n_desc: int, model):
    n_coin, n_sa, n_sb = len(rec["coincidencias"]), len(rec["solo_a"]), len(rec["solo_b"])
    union = n_coin + n_sa + n_sb
    existe = METRICS_REC_FILE.exists()
    with open(METRICS_REC_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not existe:
            w.writerow(["paciente_id", "prompt_version", "modelo", "n_a", "n_b",
                        "n_dup_a", "n_dup_b", "n_coincidencias", "n_solo_a",
                        "n_solo_b", "n_descartados", "jaccard"])
        w.writerow([paciente_id, PROMPT_VERSION, model, rec["n_a"], rec["n_b"],
                    rec["n_dup_a"], rec["n_dup_b"], n_coin, n_sa, n_sb, n_desc,
                    round(n_coin / union, 3) if union else 0])

METRICS_HUERF_FILE = METRICS_DIR / "metricas_huerfanos.csv"

def registrar_huerfanos(paciente_id: str, rec: dict):
    existe = METRICS_HUERF_FILE.exists()
    with open(METRICS_HUERF_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not existe:
            w.writerow(["paciente_id", "solo_en", "candidato_id", "dominio",
                        "cita_origen", "longitud", "texto_recorte"])
        for corrida, lista in (("a", rec["solo_a"]), ("b", rec["solo_b"])):
            for c in lista:
                t = c.get("texto_literal", "")
                w.writerow([paciente_id, corrida, c.get("candidato_id"),
                            c.get("dominio"), c.get("cita_origen"), len(t),
                            t[:150].replace("\n", " ")])

# --------------------------------------------------------------------- #
# Etapas
# --------------------------------------------------------------------- #

def ejecutar_reconocimiento(paciente_id: str, contenido: str, prompt_base: str, etapa: str,model) -> list:
    meta = llamar_claude(f"{prompt_base}\n\n{contenido}", model)
    salida = extract_json_block(meta.get("result", ""))
    escribir_salida(salida, output_path / etapa / f"{paciente_id}.json")
    registrar_metrica(paciente_id, etapa, meta, meta.get("duration_ms", 0) / 1000, model)
    return salida.get("candidatos", [])


def ejecutar_extraccion_fina(paciente_id: str, indicios: list, prompt_fino: str,model) -> dict:
    prompt = (f"{prompt_fino}\n\n"
              f"--- INDICIOS DETECTADOS ---\n{json.dumps(indicios, ensure_ascii=False)}\n") 
    meta = llamar_claude(prompt, model)
    resultado = extract_json_block(meta.get("result", ""))
    resultado, colapsos = deduplicar_salida(resultado= resultado)
    registrar_colapsos(paciente_id= paciente_id, colapsos=colapsos)
    escribir_salida(resultado, output_path / "final" / f"{paciente_id}.json")
    registrar_metrica(paciente_id, "extraccion_fina", meta, meta.get("duration_ms", 0) / 1000,model)
    return resultado


def procesar_paciente(archivo: Path, prompts: dict) -> dict: #procesa todos los pacientes 
    pid = archivo.stem  # raíz identificador del archivo
    contenido = archivo.read_text(encoding="utf-8")          # única lectura de disco
    citas = json.loads(contenido).get("citas", [])
    citas_norm = {c["numero"]: normalizar(c["texto"]) for c in citas}   # normaliza 1 vez

    a = ejecutar_reconocimiento(pid, contenido, prompts["reconocimiento"], "reconocimiento_a", model= MODEL_RECONOCIMIENTO)
    b = ejecutar_reconocimiento(pid, contenido, prompts["reconocimiento"], "reconocimiento_b", model = MODEL_RECONOCIMIENTO)

    rec = reconciliar_indicios(pid, a, b, citas_norm)
    fusionados, n_desc = fusionar_para_revision(rec)

    registrar_reconciliacion(pid, rec, n_desc, MODEL_RECONOCIMIENTO)
    registrar_huerfanos(pid, rec)
    escribir_salida({"indicios": fusionados}, output_path / "reconciliacion" / f"{pid}.json")
    
    CAMPOS_MODELO = {"candidato_id", "texto_literal", "dominio", "cita_origen", "fecha"}

    indicios_limpios = [{k: v for k, v in x.items() if k in CAMPOS_MODELO} for x in fusionados]

    n_alta = sum(1 for x in fusionados if x["confirmado_por"] == 2)
    print(f"  reconciliación: {n_alta} alta / {len(fusionados) - n_alta} a revisar"
           f"/ {n_desc} descartados / dup a={rec['n_dup_a']} b={rec['n_dup_b']}")
    return ejecutar_extraccion_fina(pid, indicios_limpios, prompts["extraccion_fina"],MODEL_EXTRACCION)


# --------------------------------------------------------------------- #

def extraccion_indice(ruta_csv: Path) -> set:
    ids = pd.read_csv(ruta_csv)["paciente"].unique()
    return {str(x).strip() for x in ids}

def deduplicar_salida(resultado: dict) -> tuple[dict, list]:
    """Colapsa objetos idénticos salvo candidato_id. Conserva ambos ids."""
    colapsos = []
    for clave, lista in resultado.items():
        if not isinstance(lista, list):
            continue
        vistos, unicos = {}, []
        for obj in lista:
            firma = json.dumps(
                {k: v for k, v in obj.items() if k != "candidato_id"},
                sort_keys=True, ensure_ascii=False,
            )
            if firma in vistos:
                prev = vistos[firma]
                prev.setdefault("candidato_ids_colapsados", []).append(obj["candidato_id"])
                colapsos.append((clave, obj["candidato_id"], prev["candidato_id"]))
            else:
                vistos[firma] = obj
                unicos.append(obj)
        resultado[clave] = unicos
    return resultado, colapsos

METRICS_COL_FILE = METRICS_DIR / "metricas_colapsos.csv"

def dominio_de(cid: str) -> str:
    """candidato_id = pid:corrida:cita:dominio:idx"""
    partes = cid.split(":")
    return partes[3] if len(partes) >= 4 else ""

def registrar_colapsos(paciente_id: str, colapsos: list):
    if not colapsos:
        return
    existe = METRICS_COL_FILE.exists()
    with open(METRICS_COL_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not existe:
            w.writerow(["paciente_id", "array_salida", "dominio_descartado",
                        "dominio_conservado", "candidato_id_descartado",
                        "candidato_id_conservado"])
        for clave, cid_desc, cid_cons in colapsos:
            w.writerow([paciente_id, clave, dominio_de(cid_desc),
                        dominio_de(cid_cons), cid_desc, cid_cons])

def main():
    for etapa in ETAPAS:
        (output_path / etapa).mkdir(parents=True, exist_ok=True)

    prompts = {
        "reconocimiento": PROMPT_RECONOCIMIENTO.read_text(encoding="utf-8"),
        "extraccion_fina": PROMPT_EXTRACCION.read_text(encoding="utf-8"),
    }

    indices = extraccion_indice(marcados)
    print(f"{len(indices)} paciente(s) en lista de trabajo")

    pendientes = [f for f in sorted(path.glob("*.json"))
                  if f.stem in indices
                  and not (output_path / "final" / f"{f.stem}.json").exists()]

    print(f"{len(pendientes)} paciente(s) pendientes")
    if not pendientes:
        return 0

    ok = fallos = 0
    for i, archivo in enumerate(pendientes, 1):
        print(f"[{i}/{len(pendientes)}] {archivo.name}...")
        try:
            procesar_paciente(archivo, prompts)
            ok += 1
        except SinAutenticacion as e:
            print(f"\n>>> SIN AUTENTICACIÓN: {e}")
            print(">>> claude no tiene credenciales válidas en esta sesión. "
                  "Ejecuta 'claude setup-token' y guarda el valor como "
                  "CLAUDE_CODE_OAUTH_TOKEN en el .env")
            print(f">>> Quedan {len(pendientes) - i + 1} pendientes; se aborta sin gastarlos")
            return 76
        except LimiteAlcanzado as e:
            print(f"\n>>> LÍMITE ALCANZADO: {e}")
            print(f">>> Quedan {len(pendientes) - i + 1} pendientes; la próxima corrida los retoma")
            return 75
        except Exception as e:
            print(f"  Falló: {e}")
            fallos += 1

    print(f"\nListo: {ok} ok, {fallos} fallos")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())