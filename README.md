# BDMEP Downloader

Ferramenta web não oficial para automatizar o download de dados climáticos do [BDMEP (Banco de Dados Meteorológicos para Ensino e Pesquisa)](https://bdmep.inmet.gov.br) do INMET.

> ⚠️ **Aviso:** Este projeto não tem vínculo com o INMET. Os dados são fornecidos pelo INMET via BDMEP. Não me responsabilizo pela integridade ou disponibilidade dos dados obtidos por esta ferramenta.

## Acesso online

Acesse diretamente sem instalar nada:

👉 **[bdmep.ruiogawa.net](https://bdmep.ruiogawa.net)**

## Motivação

O processo de download de dados no BDMEP envolve várias etapas manuais:

1. Preencher um formulário extenso (tipo de estação, variáveis, período, formato)
2. Aguardar um e-mail de confirmação
3. Clicar no link do e-mail
4. Aguardar o processamento
5. Baixar o arquivo ZIP

Esta ferramenta automatiza todas essas etapas, bastando preencher o formulário uma única vez.

## Funcionalidades

- Seleção de estações convencionais (M) ou automáticas (T)
- Filtro de variáveis disponíveis por tipo de estação
- Seleção de período (data inicial e final)
- Download automático do arquivo ZIP com os dados
- Log em tempo real do processo de automação

## Como usar

1. Selecione o **tipo de estação** (Convencional ou Automática)
2. Escolha a **estação** pelo código ou nome
3. Marque as **variáveis** desejadas
4. Defina o **período** (data inicial e final)
5. Selecione o **formato** de saída
6. Clique em **Baixar Dados** e aguarde o download automático

O log exibido na tela mostra cada etapa do processo em tempo real. O download começa automaticamente assim que o arquivo estiver pronto.

## Executar localmente no seu computador

Prefere rodar no próprio computador? Basta ter Python instalado — não é necessário nenhuma configuração de servidor.

### Pré-requisitos

- [Python 3.10 ou superior](https://www.python.org/downloads/)
- pip (incluído com o Python)

### Linux e macOS

```bash
# 1. Clone o repositório
git clone https://github.com/ruiogawa/bdmep-downloader.git
cd bdmep-downloader

# 2. Crie um ambiente virtual
python3 -m venv venv
source venv/bin/activate

# 3. Instale as dependências
pip install flask requests playwright
playwright install chromium

# 4. Execute
python3 Bdmep_app.py
```

Abra o navegador em: **http://localhost:5000**

### Windows

```bat
:: 1. Clone o repositório (ou baixe o ZIP e extraia)
git clone https://github.com/ruiogawa/bdmep-downloader.git
cd bdmep-downloader

:: 2. Crie um ambiente virtual
python -m venv venv
venv\Scripts\activate

:: 3. Instale as dependências
pip install flask requests playwright
playwright install chromium

:: 4. Execute
python Bdmep_app.py
```

Abra o navegador em: **http://localhost:5000**

> **Nota Windows:** se `playwright install-deps` solicitar dependências adicionais do sistema, execute o comando com permissão de administrador.

### Encerrando

Para parar o servidor, pressione `Ctrl+C` no terminal onde o app está rodando.

### Com Docker

Se preferir usar Docker:

```bash
git clone https://github.com/ruiogawa/bdmep-downloader.git
cd bdmep-downloader
docker compose up -d
```

Acesse em: **http://localhost:5010**

Para parar: `docker compose down`

## Detalhes técnicos

A ferramenta utiliza automação de navegador (Playwright + Chromium headless) para interagir com o site do BDMEP, contornando a proteção anti-bot que impede chamadas diretas à API. O processo completo — preenchimento do formulário, confirmação e download do arquivo — é executado automaticamente em segundo plano.

## Dependências

- [Flask](https://flask.palletsprojects.com/) — servidor web
- [Playwright](https://playwright.dev/python/) — automação de navegador
- [Requests](https://docs.python-requests.org/) — requisições HTTP

## Autor

Desenvolvido por **Rui Ogawa**  
📧 ruiogawa@gmail.com  
🐙 [github.com/ruiogawa/bdmep-downloader](https://github.com/ruiogawa/bdmep-downloader)

## Licença

MIT
