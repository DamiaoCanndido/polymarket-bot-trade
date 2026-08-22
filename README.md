# 🚀 Polymarket Copytrading Bot

Um robô automatizado para **copiar os melhores traders da Polymarket** em tempo real, com painel visual no navegador e gestão rígida de risco para proteger o seu capital.

> 💡 **Não entende nada de programação?** Não se preocupe! Este guia foi feito passo a passo para qualquer pessoa instalar e rodar no **Windows** em poucos minutos.

---

## ⚡ Início Rápido no Windows (Modo 1-Clique)

Se você já tem o Python instalado no seu computador:

1. Dê um duplo clique no arquivo **[`iniciar_dashboard.bat`](file:///home/nergal/apps/polymarket/iniciar_dashboard.bat)**.
2. Ele vai criar o ambiente, instalar tudo o que precisa e abrir o painel automaticamente.
3. Abra o seu navegador e acesse: **`http://localhost:5000`**.

---

## 🛠️ Passo a Passo Completo de Instalação no Windows

Se você está começando do zero absoluto no Windows, siga as etapas abaixo:

---

### Passo 1: Instalar o Python no Windows

1. Acesse o site oficial: [python.org/downloads](https://www.python.org/downloads/)
2. Clique no botão amarelo **Download Python** (versão 3.10 ou superior).
3. Abra o instalador baixado.
4. ⚠️ **MUITO IMPORTANTE (Não pule esta parte!)**:
   - Na primeira tela de instalação, marque a caixinha: **`☑ Add python.exe to PATH`** (ou *Adicionar Python ao PATH*).
   - Depois clique em **Install Now**.
5. Quando terminar, clique em **Close**.

---

### Passo 2: Abrir o Terminal na Pasta do Bot

1. Abra a pasta onde os arquivos deste bot estão salvos no seu Windows.
2. Clique na **barra de endereços** do topo da pasta (onde mostra o caminho, ex.: `C:\Users\SeuNome\polymarket`).
3. Digite **`cmd`** e aperte a tecla **Enter**.
4. Uma janela preta (Prompt de Comando) vai se abrir já dentro da pasta correta!

---

### Passo 3: Criar o Ambiente e Instalar as Dependências

Na janela preta que você abriu, digite (ou copie e cole) os comandos abaixo, apertando **Enter** após cada linha:

1. **Criar o ambiente isolado do Python**:
   ```cmd
   python -m venv .venv
   ```

2. **Ativar o ambiente**:
   ```cmd
   .venv\Scripts\activate
   ```
   *(Você verá um `(.venv)` aparecer no início da linha do terminal)*.

3. **Instalar tudo o que o bot precisa**:
   ```cmd
   pip install -r requirements.txt
   ```

---

### Passo 4: Configurar o seu Arquivo de Preferências (`.env`)

1. Crie o seu arquivo de configuração copiando o modelo de exemplo:
   ```cmd
   copy .env.example .env
   ```
2. Abra o arquivo `.env` no **Bloco de Notas** ou no seu editor preferido.
3. Entenda e ajuste os valores principais:

| Variável | O que significa? | Valor Recomendado para Iniciar |
| :--- | :--- | :--- |
| `DRY_RUN` | `true` = Dinheiro de treino (simulação). `false` = Dinheiro real. | `true` |
| `FIXED_AMOUNT_USD` | Valor em dólares que você vai colocar em cada aposta copiada. | `5.0` (cinco dólares) |
| `DAILY_BUDGET_USD` | Limite máximo diário total que o bot pode gastar por dia. | `50.0` |
| `MAX_PER_MARKET_USD` | Limite máximo em um único mercado de aposta. | `20.0` |
| `SLIPPAGE_TOLERANCE_PCT` | Tolerância máxima de variação de preço (evita pagar caro). | `2.0` (2%) |
| `AUTO_EXIT_ON_SELL` | Se o trader mestre vender, você vende junto proporcionalmente. | `true` |

---

### Passo 5: Conectar sua Carteira Polymarket (Bullpen)

O bot utiliza a ferramenta segura da **Bullpen** para conectar sua carteira à Polymarket na rede Polygon.

Você tem duas formas de fazer isso:

#### 🟢 Opção 1: Automática com 1-Clique (Mais Fácil para Leigos)
1. Dê um duplo clique no arquivo **[`configurar_carteira_bullpen.bat`](file:///home/nergal/apps/polymarket/configurar_carteira_bullpen.bat)**.
2. O script vai:
   - Baixar e instalar o Bullpen automaticamente se ainda não estiver instalado.
   - Abrir o seu navegador para você fazer o login da Polymarket.
   - Aprovar as permissões de trading na rede Polygon automaticamente.
   - Testar sua carteira e confirmar que está tudo pronto.

---

#### 🔵 Opção 2: Manual pelo Terminal (Alternativa)
Se preferir fazer manualmente pelo terminal:

1. **Instalar o Bullpen no Windows** (abra o PowerShell e cole):
   ```powershell
   irm https://cli.bullpen.fi/install.ps1 | iex
   ```
2. **Executar a Configuração Guiada**:
   ```cmd
   bullpen setup
   ```
3. **Verificar se está tudo OK**:
   ```cmd
   bullpen polymarket preflight
   ```


---

#### C. Comandos Úteis do Bullpen

- **Verificar se tudo está pronto e conferir saldo**:
  ```cmd
  bullpen polymarket preflight
  ```
- **Depositar saldo (USDC / pUSD) na sua carteira de trading**:
  ```cmd
  bullpen deposit
  ```
- **Diagnosticar autenticação e chaves**:
  ```cmd
  bullpen doctor auth
  ```

---

#### D. Ajustar o Caminho no `.env` (se necessário)
Se o comando `bullpen` estiver no seu PATH do Windows, o seu arquivo [`.env`](file:///home/nergal/apps/polymarket/.env) pode ficar simplesmente:
```env
BULLPEN_PATH=bullpen
```
Ou com o caminho completo onde o executável foi instalado (ex.: `C:\Users\SeuUsuario\.bullpen\bin\bullpen.exe`).

---

### Passo 6: Iniciar o Painel Visual (Dashboard)

Agora basta iniciar o servidor web do bot:

```cmd
python bot.py dashboard
```

Ou simplesmente dê um duplo clique no arquivo **[`iniciar_dashboard.bat`](file:///home/nergal/apps/polymarket/iniciar_dashboard.bat)**!

👉 **Abra no seu navegador de internet**: [http://localhost:5000](http://localhost:5000)

---

## 🖥️ Como Usar o Dashboard Web

O painel visual no seu navegador é muito simples e intuitivo:

1. **Botão Iniciar / Parar**: Ligue ou pause o bot com um único clique.
2. **Modo Paper vs Live**:
   - **Paper Trading (Simulação)**: Teste estratégias com dinheiro virtual sem risco de perder fundos reais.
   - **Live Execution (Dinheiro Real)**: O bot executará compras e vendas reais na sua carteira Polymarket.
3. **Métricas em Tempo Real**: Veja seu Lucro/Prejuízo (PnL), Taxa de Acerto (Win Rate %), Saldo em Caixa e Patrimônio.
4. **Feed de Trades**: Acompanhe todas as compras e vendas copiadas em tempo real com detalhes de preço e volume.
5. **Top 25 Traders**: Você pode pausar ou ativar traders individualmente clicando no botão ao lado do nome deles.

---

## 🔍 Como Atualizar os Melhores Traders (Scanner)

Para fazer o robô vasculhar o ranking da Polymarket e selecionar os **25 traders mais lucrativos e consistentes** dos últimos 7 dias:

```cmd
python bot.py scan --period 7d --top 25 --save
```

Eles serão salvos automaticamente e ficarão visíveis no painel web.

---

## 💰 Dicas de Ouro para Operar com Dinheiro Real

1. **Comece Sempre em Modo Simulação (`DRY_RUN=true`)**: Deixe o bot rodando por 1 ou 2 dias no modo simulação para ver como os traders escolhidos operam.
2. **Comece Pequeno no Modo Real**: Ao ativar `DRY_RUN=false`, configure `$2.00` a `$5.00` por trade no início.
3. **Controle de Slippage**: O bot vem configurado com proteção de 2% de slippage. Isso impede que você compre uma aposta que já disparou de preço após a entrada do mestre.
4. **Venda Automática Proporcional**: Se o trader mestre vender 50% da posição dele, o bot detecta e vende automaticamente 50% da sua posição equivalente.

---

## ❓ Perguntas Frequentes & Solução de Problemas no Windows

#### 1. "O comando 'python' não é reconhecido..."
> **Solução**: Você não marcou a opção **"Add Python to PATH"** ao instalar o Python. Desinstale o Python, baixe novamente pelo site oficial e certifique-se de marcar essa caixinha na primeira tela.

#### 2. "A execução de scripts foi desabilitada neste sistema" (Erro no PowerShell)
> **Solução**: O Windows bloqueia scripts por padrão no PowerShell. Para liberar, abra o PowerShell como Administrador e execute:
> ```powershell
> Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```
> Ou simplesmente use o **Prompt de Comando tradicional (`cmd`)** ou o arquivo `.bat`.

#### 3. "Porta 5000 já está em uso"
> **Solução**: Você pode rodar o dashboard em outra porta, por exemplo na 5050:
> ```cmd
> python dashboard.py 5050
> ```
> E abrir no navegador: `http://localhost:5050`.

#### 4. Como fechar ou encerrar o bot?
> **Solução**: Vá na janela preta do terminal onde o bot está rodando e aperte as teclas **Ctrl + C** no teclado (ou simplesmente feche a janela do terminal).

---

## 📁 Estrutura dos Arquivos

```text
polymarket/
├── iniciar_dashboard.bat    # Executável de 1-clique para Windows
├── bot.py                   # Ponto de entrada principal do bot
├── dashboard.py             # Painel Web visual (Flask + Tailwind + Chart.js)
├── config.py                # Leitor de configurações e do arquivo .env
├── config.json              # Configurações salvas e lista dos traders ativos
├── .env                     # Suas configurações e chaves privadas locais (protegido)
├── .env.example             # Modelo explicativo das variáveis de ambiente
├── scanner.py               # Robô que busca os melhores traders da Polymarket
├── risk_manager.py          # Gestor de risco (limites diários, slippage, etc.)
├── executor.py              # Executor de ordens via Bullpen CLI
├── tracker.py               # Monitor de feed de trades e cálculo proporcional
├── trades_log.jsonl         # Histórico de todas as operações realizadas
├── portfolio_state.json     # Saldo, posições abertas e PnL atualizados
└── requirements.txt         # Lista de programas auxiliares em Python
```
