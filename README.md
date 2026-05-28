# ⚡ GuardaTensão v3

Monitor de energia em tempo real para Linux — painel industrial no terminal, estética rack elétrico âmbar.

```
 ▄▄  ▄  ▄  ▄▄  ▄▄  ▄▄  ▄▄     ▄▄  ▄▄  ▄▄  ▄▄ ▄  ▄  ▄▄  ▄▄
█    █  █ █  █ █   █  █ █  █   █   █   █  █ █ █  █ █  █ █
█ ▄▄ █  █ █▄▄█ █▄▄ █  █ █▄▄█   █▄▄ █▄▄ █  █ █  ▀▀  ██▄█ █▄▄
█  █ █  █ █  █ █   █  █ █  █   █   █   █  █ █   █  █  █ █
 ▀▀   ▀▀  █  █ ▀▀   ▀▀  █  █   ▀▀  ▀▀   ▀▀  ▀   █  █  █ ▀▀
```

---

## O que é

GuardaTensão v3 é um monitor de energia e sistema para Linux que roda no terminal via `curses`. Detecta quedas de energia, oscilações na rede elétrica e instabilidade de corrente — com alertas visuais, sonoros e notificações do sistema — enquanto exibe um painel completo de hardware em tela cheia.

A v3 reformulou o layout do zero: três colunas que ocupam toda a tela, gráficos históricos em tempo real, dados de hardware que a v2 não mostrava, e log de eventos sempre visível na base.

---

## Requisitos

- Linux (kernel com `/sys/class/power_supply/`)
- Python 3.8+
- `psutil`

```bash
# pip
pip install psutil

# ou no Fedora/RHEL
sudo dnf install python3-psutil

# ou no Debian/Ubuntu
sudo apt install python3-psutil
```

`notify-send` é opcional — usado para notificações de desktop. Sem ele, tudo funciona normalmente.

---

## Como usar

```bash
python3 gt_v3.py
```

Recomenda-se rodar em tela cheia. O painel se adapta a qualquer resolução, mas quanto maior o terminal, mais informação aparece.

### Atalhos

| Tecla | Ação |
|-------|------|
| `Q` / `Esc` | Encerrar |
| `B` | Ligar/desligar beep sonoro |
| `L` | Limpar log de eventos |
| `R` | Resetar estatísticas da sessão |

---

## Painel — layout em 3 colunas

### Coluna A — Bateria & Energia
- Nível de carga (%) com barra visual
- Tensão (V), corrente (A) e potência (W) instantâneas
- Tempo restante estimado
- Status de carga (Carregando / Descarregando / Completa)
- Status da rede AC (Online / Offline)
- Capacidade atual vs. máxima em mAh
- **Saúde da bateria** (%) — capacidade atual ÷ design original
- Pico de corrente da sessão
- Mín/máx de tensão registrados
- Energia acumulada consumida na sessão (mWh)
- Ciclos de carga contados na sessão
- Metadados do hardware: fabricante, modelo, tecnologia, ciclos totais do kernel, temperatura da bateria

### Coluna B — Oscilação & Histórico
- **Barra de instabilidade** de 0–100% (verde → amarelo → vermelho)
- Contador de eventos de plug/unplug na janela de 5 minutos
- Indicadores: ciclo rápido detectado, corrente instável
- Estatísticas da sessão: total de quedas, oscilações, avisos, maior queda
- Três gráficos sparkline em tempo real:
  - % de bateria
  - Corrente (A)
  - Tensão (V)

### Coluna C — Sistema
- Uso total de CPU com barra visual
- Frequência atual, mínima e máxima
- Contagem de núcleos físicos e lógicos
- Temperatura da CPU
- Uso por núcleo (até 16 cores exibidos)
- Gráfico histórico de CPU
- RAM usada/total com barra e percentual
- Swap usada/total
- Gráfico histórico de RAM
- Taxa de leitura e escrita de disco (B/s, KB/s, MB/s…)
- Taxa de envio e recebimento de rede
- Uso de cada partição montada (até 4)
- Top 5 processos por CPU com PID, nome, CPU% e MEM%

### Log de eventos
Faixa na parte inferior da tela com histórico colorido de todos os eventos detectados: quedas, retornos de energia, oscilações rápidas, variações de corrente, alertas de bateria baixa/crítica.

O log também é salvo em `~/.guardatensao.log`.

---

## Detecção de oscilação

O algoritmo monitora três padrões independentes:

**Oscilação por eventos** — conta quantas vezes a energia caiu e voltou nos últimos 5 minutos. Se ≥ 3 eventos nessa janela, o score sobe e o alerta dispara.

**Ciclo rápido** — se a energia voltou em menos de 8 segundos após cair, é classificado como oscilação rápida (transitório elétrico).

**Instabilidade de corrente** — monitora a variação entre leituras consecutivas de corrente. Uma variação ≥ 0,15 A enquanto plugado indica instabilidade de carga na rede.

O **score de instabilidade** (0–100%) combina esses fatores e muda a cor do medidor: verde (estável), amarelo (atenção), vermelho (instável — considere um nobreak).

---

## Alertas

| Evento | Beep | Notificação | Flash |
|--------|------|-------------|-------|
| Queda de energia | 2× | ⚠ crítica | sim |
| Energia voltou | 1× | normal | sim |
| Oscilação rápida | 3× | ⚠ crítica | sim |
| Rede instável | 3× | ⚠ crítica | sim |
| Bateria baixa (≤ 20%) | 1× | normal | — |
| Bateria crítica (≤ 10%) | 3× | ⚠ crítica | sim |
| Queda brusca (≥ 5%) | 1× | — | — |

---

## Configuração

As constantes no topo do arquivo permitem ajustar o comportamento sem mexer na lógica:

```python
REFRESH_RATE       = 0.8    # intervalo de atualização em segundos
HISTORY_SIZE       = 120    # pontos nos gráficos históricos
ALERT_DROP_PCT     = 5      # queda brusca de X% aciona alerta
ALERT_LOW_PCT      = 20     # bateria baixa
CRITICAL_LOW_PCT   = 10     # bateria crítica
OSCIL_WINDOW_SECS  = 300    # janela de observação para oscilação (segundos)
OSCIL_EVENT_THRESH = 3      # eventos nessa janela = rede instável
OSCIL_RAPID_SECS   = 8      # queda+retorno em X seg = ciclo rápido
CURRENT_VAR_THRESH = 0.15   # variação de corrente (A) para detectar instab.
SAVE_LOG           = True
LOG_FILE           = "~/.guardatensao.log"
```

---

## Compatibilidade

Testado em Fedora / Arch / Ubuntu com notebooks que expõem `/sys/class/power_supply/`. Em máquinas sem bateria (desktops, VMs) o painel exibe as colunas de sistema normalmente, sem a seção de bateria.

Terminais recomendados: `kitty`, `alacritty`, `gnome-terminal`, `konsole`. O painel usa caracteres Unicode block — certifique-se de ter uma fonte com suporte (Nerd Fonts, JetBrains Mono, Fira Code, etc.).

---

## Log

Todos os eventos são registrados em `~/.guardatensao.log` com timestamp completo:

```
[2025-08-14 23:47:02][START] GuardaTensão v3 iniciado
[2025-08-14 23:51:18][OUTAGE] QUEDA — bat 87%
[2025-08-14 23:51:24][OSCIL] OSCILAÇÃO RÁPIDA! ciclo 6.1s
[2025-08-14 23:51:24][POWER] Energia RETORNOU (fora 6s) — bat 87%
```

---

## Licença

MIT — use, modifique e distribua à vontade.
