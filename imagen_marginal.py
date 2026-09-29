"""imagen_marginal.py — Imagen del reporte diario de predespacho (WhatsApp).

Izquierda: costo marginal horario en escalones, con el fondo coloreado por el combustible de la
planta marginal (mismo diseño que la pestaña Despacho del dashboard IndicadoresMEM).
Derecha: tabla con el periodo, el costo marginal y la planta marginal de cada hora.
"""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

# Paleta por combustible (igual a utils/viz_marginal.py del dashboard)
COLOR = {"Líquidos": "#E0527F", "Gas": "#C4901C", "Carbón": "#9A5220", "Agua": "#2F7FC1",
         "Biomasa/Otros": "#D19FE8", "Viento": "#129BAA", "Sol": "#62B041", "Sin clasificar": "#9E9E9E"}
ORDEN = ["Líquidos", "Gas", "Carbón", "Agua", "Biomasa/Otros", "Viento", "Sol", "Sin clasificar"]

FONDO, PANEL, TINTA, TINTA_2, REJILLA, ACENTO = "#1a1b26", "#24283b", "#c0caf5", "#8b93b8", "#414868", "#7aa2f7"


def pesos(v: float) -> str:
    """1114.25 -> '$ 1.114,25' (formato colombiano)."""
    return "$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _tramos(serie):
    out, ini = [], 0
    for i in range(1, len(serie) + 1):
        if i == len(serie) or serie[i] != serie[ini]:
            out.append((serie[ini], ini, i - 1))
            ini = i
    return out


def generar_imagen(horario, fecha: str, ruta_png: str, escasez_sup: float | None = None) -> str:
    """horario: DataFrame con hora (1..24), costo_marginal (COP/MWh) y planta_marginal / grupo_combustible."""
    h = horario.sort_values("hora").reset_index(drop=True)
    cm = (h["costo_marginal"] / 1000.0).tolist()
    grupos = h["grupo_combustible"].fillna("Sin clasificar").tolist()
    plantas = h["planta_marginal"].fillna("—").tolist()
    x = list(range(24))
    etiquetas = [f"{i:02d}-{i + 1:02d}h" for i in range(24)]
    promedio = sum(cm) / 24

    plt.rcParams["font.family"] = "DejaVu Sans"
    fig = plt.figure(figsize=(17, 8.2), facecolor=FONDO)
    gs = fig.add_gridspec(1, 2, width_ratios=[2.25, 1.25], wspace=0.04)

    # ---------------- Gráfico ----------------
    ax = fig.add_subplot(gs[0, 0], facecolor=FONDO)
    tope = max(max(cm), escasez_sup or 0)
    rango = max(tope - min(cm), 50)
    y0, y1 = min(cm) - rango * 0.15, tope + rango * 0.30
    for g, ini, fin in _tramos(grupos):
        ax.axvspan(ini - 0.5, fin + 0.5, color=COLOR.get(g, COLOR["Sin clasificar"]), alpha=0.20, lw=0, zorder=0)
        n = fin - ini + 1
        ax.text((ini + fin) / 2, y1 - rango * 0.03, f"{g}\n{n} h" if n >= 2 else f"{n} h",
                ha="center", va="top", fontsize=10.5 if n >= 2 else 9,
                color=TINTA if n >= 2 else TINTA_2, fontweight="bold" if n >= 2 else "normal", zorder=3)
    if escasez_sup:
        ax.axhline(escasez_sup, color="#f43f5e", lw=1.6, ls=(0, (6, 4)), alpha=0.75, zorder=2)
        ax.text(23.4, escasez_sup + rango * 0.015, f"Esc. Sup. {pesos(escasez_sup)}", ha="right", va="bottom",
                fontsize=10, color="#f7768e", zorder=3)
    ax.plot(x, cm, drawstyle="steps-post", color="#e5e9f0", lw=2.6, zorder=4)
    ax.plot([23, 23.5], [cm[-1], cm[-1]], color="#e5e9f0", lw=2.6, zorder=4)  # cierra el último escalón
    ax.scatter(x, cm, s=70, c=[COLOR.get(g, COLOR["Sin clasificar"]) for g in grupos],
               edgecolors=FONDO, linewidths=1.8, zorder=5)

    ax.set_xlim(-0.5, 23.5)
    ax.set_ylim(y0, y1)
    ax.set_xticks(x)
    ax.set_xticklabels(etiquetas, rotation=45, ha="right", fontsize=9.5, color=TINTA_2)
    ax.tick_params(axis="y", colors=TINTA_2, labelsize=10)
    ax.tick_params(axis="x", length=0)
    ax.set_ylabel("Costo marginal (COP/kWh)", fontsize=12, color=TINTA)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}".replace(",", ".")))
    ax.grid(axis="y", color=REJILLA, ls="--", alpha=0.6, zorder=1)
    for lado in ("top", "right"):
        ax.spines[lado].set_visible(False)
    for lado in ("bottom", "left"):
        ax.spines[lado].set_color(REJILLA)

    presentes = [g for g in ORDEN if g in set(grupos)]
    ax.legend(handles=[Line2D([0], [0], marker="o", ls="", markersize=9, markerfacecolor=COLOR[g],
                              markeredgecolor=FONDO, label=g) for g in presentes],
              loc="upper left", bbox_to_anchor=(0, -0.13), ncol=len(presentes), frameon=False,
              fontsize=10.5, labelcolor=TINTA, handletextpad=0.3, columnspacing=1.4,
              title="Combustible de la planta marginal:", title_fontsize=10.5, alignment="left")
    ax.get_legend().get_title().set_color(TINTA_2)
    ax.set_title(f"Predespacho XM - {fecha}  (Promedio: {pesos(promedio)}/kWh)",
                 fontsize=17, color=TINTA, pad=16)

    # ---------------- Tabla ----------------
    axt = fig.add_subplot(gs[0, 1])
    axt.axis("off")
    filas = [[etiquetas[i], pesos(cm[i]), "", plantas[i]] for i in range(24)]
    tabla = axt.table(cellText=filas, colLabels=["Periodo", "COP/kWh", "", "Planta marginal"],
                      colWidths=[0.21, 0.25, 0.035, 0.505], loc="center", cellLoc="center")
    tabla.auto_set_font_size(False)
    tabla.set_fontsize(10.5)
    tabla.scale(1, 1.52)
    for (i, j), celda in tabla.get_celld().items():
        celda.set_edgecolor(REJILLA)
        if i == 0:
            celda.set_facecolor(PANEL)
            celda.set_text_props(weight="bold", color=ACENTO)
            continue
        celda.set_facecolor(FONDO)
        celda.set_text_props(color=TINTA)
        if j == 2:  # muestra del color del combustible
            celda.set_facecolor(COLOR.get(grupos[i - 1], COLOR["Sin clasificar"]))
        if j == 3:
            celda.set_text_props(ha="left", color=TINTA)
            celda.PAD = 0.04

    fig.text(0.99, 0.01, "Fuente: XM (iMAR, PrId, OFEI). Planta marginal inferida por Enerconsult.",
             fontsize=9, color=TINTA_2, ha="right")
    fig.savefig(ruta_png, dpi=130, bbox_inches="tight", facecolor=FONDO)
    plt.close(fig)
    return ruta_png
