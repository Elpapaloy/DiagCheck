
import re, json, subprocess, sys, time, os
import pandas as pd 
from dotenv import load_dotenv
from pathlib import Path
from difflib import SequenceMatcher

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

def _req(nombre: str) -> Path:
    v = os.getenv(nombre)
    if not v:
        sys.exit(f"ERROR: falta {nombre} en {env_path}")
    return Path(v)

path        = _req("source_path")
output_path = _req("output_path")
claude_bin  = _req("CLAUDE_BIN")
marcados = _req("MARCADOS")
PROMTP_FILE = _req("PROMPT_FILE")


if not claude_bin.is_file():
    sys.exit(f"ERROR: {claude_bin} no es un archivo (¿falta claude.exe?)")


MARCADORES_LIMITE = ["session limit", "hit your session limit"]

class LimiteAlcanzado(Exception): pass

def extraccion_indice(path: Path):
    indices = pd.read_csv(r"C:\Users\Marco\OneDrive\Desktop\historias_automatizacion\DiagCheck\lista_exclusion.csv")
    indices = indices["paciente"].unique()
    print(indices)
    return indices
import tempfile

def llamar_claude(prompt: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp_cwd:
        proc = subprocess.run(
            [str(claude_bin), "-p", "--output-format", "json",
            "--permission-mode", "dontAsk",
            "--allowedTools", "",
            "--max-turns", "1",
            "--model", "claude-haiku-5"],
            input=prompt,
            capture_output=True,
            encoding="utf-8",
            text=True,
            timeout=1000,
            cwd= tmp_cwd,
        )
## tal vez valga la pena implementar la persistencia 0 de datos de las llamadas en el disco: 
        try:
            meta = json.loads(proc.stdout)
            if meta.get("permission_denials"):
                print(f"  (herramientas bloqueadas correctamente: {meta['permission_denials']})")
        except json.JSONDecodeError:
            raise RuntimeError(
                f"salida no parseable (rc={proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()[:300]}"
            )

        if meta.get("is_error"):
            mensaje = meta.get("result", "")
            if any(m in mensaje.lower() for m in MARCADORES_LIMITE):
                raise LimiteAlcanzado(mensaje[:200])
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
                candidate = text[start : i + 1]
                return json.loads(candidate)
 
    raise ValueError("No se encontró un bloque JSON balanceado en la salida")

def escribir_salida(datos: dict, output_dir: Path):
    tmp= output_dir.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(datos, ensure_ascii=False, indent =2), encoding = "utf-8")
    tmp.replace(output_dir)


import csv

METRICS_FILE = Path(__file__).resolve().parent / "metricas.csv"

def registrar_metrica(archivo: str, meta: dict, duracion_s: float):
    usage = meta.get("usage", {})
    tokens_entrada = (
        usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
    )
    tokens_salida = usage.get("output_tokens", 0)

    existe = METRICS_FILE.exists()
    with open(METRICS_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if not existe:
            w.writerow(["archivo", "costo_usd", "duracion_s",
                        "tokens_entrada", "tokens_salida", "ratio"])
        ratio = tokens_entrada / tokens_salida if tokens_salida else 0
        w.writerow([archivo, meta.get("total_cost_usd", 0), round(duracion_s, 1),
                    tokens_entrada, tokens_salida, round(ratio, 1)])


def main():
    output_path.mkdir(parents=True, exist_ok=True)
    prompt_base= PROMTP_FILE.read_text(encoding= "utf-8")


    indices = extraccion_indice(marcados)
    pendientes = [f for f in sorted(path.glob("*.json")) 
                if not (output_path / f"{f.stem}.json").exists() 
                and ((f"{f.stem}") in indices) 
                ]
    print(f"pendientes: {pendientes}")
    
    print(f"{len(pendientes)} paciente(s) pendientes")

    if not pendientes:
        return 0
    ok = fallos = 0 

    for i, archivo in enumerate(pendientes,1):
        contenido = archivo.read_text(encoding= "utf-8")
        prompt= (
                f"{prompt_base}\n\n"
                f"{contenido}"
            )

        print(f"{i}/{len(pendientes)} {archivo.name}...")
        try: 
            meta  = llamar_claude(prompt)
            usage = meta.get("usage", {})
            print("input_tokens:", usage.get("input_tokens"))
            print("cache_creation_input_tokens:", usage.get("cache_creation_input_tokens"))  # CARO — contenido nuevo, nunca visto
            print("cache_read_input_tokens:", usage.get("cache_read_input_tokens"))          # BARATO — reutilizado de caché
            cc = usage.get("cache_creation", {})
            print("ephemeral_5m:", cc.get("ephemeral_5m_input_tokens"))
            print("ephemeral_1h:", cc.get("ephemeral_1h_input_tokens"))
            datos = extract_json_block(meta.get("result",""))
            escribir_salida(datos, output_path / f"{archivo.stem}.json")
            duracion_s = meta.get("duration_ms", 0) / 1000
            registrar_metrica(archivo.name, meta, duracion_s)
            print(f" OK ${float(meta.get('total_cost_usd', 0)):.4f}  ({duracion_s:.1f}s)")
            ok += 1

        except LimiteAlcanzado as e:
            print(f"LIMITE ALCANZADO:  {e}")
            print(f">>> Quedan {len(pendientes)-i+1} pendientes")
            print(f"La proxima corrida los retoma")
            return 75

        except Exception as e:
            print(f"Falló: {e}")
            fallos += 1

    print(f"Listo {ok} ok, {fallos} fallos ")
    return 1 if fallos else 0 


if __name__ == "__main__":
    sys.exit(main())


    

    
