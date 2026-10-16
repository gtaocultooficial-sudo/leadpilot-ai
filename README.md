# LeadPilot AI — MVP protegido

Aplicação independente para registrar e acompanhar oportunidades comerciais. Não faz parte do GTA Oculto AI.

## Recursos desta versão
- Painel de leads, métricas e status comercial.
- Cadastro/edição, busca e filtro de leads.
- Pontuação inicial baseada nas informações disponíveis.
- Rascunhos de abordagem com revisão humana; não envia mensagens automaticamente.
- Importação/exportação CSV.
- Login obrigatório para as páginas comerciais.
- PostgreSQL para persistência.

## Configuração obrigatória
O aplicativo **falha ao iniciar** se as variáveis essenciais não estiverem definidas, evitando publicar uma versão sem autenticação ou banco.

- `DATABASE_URL`: URL de conexão do PostgreSQL.
- `SECRET_KEY`: segredo forte e aleatório para sessões Flask.
- `APP_USERNAME`: usuário de acesso.
- `APP_PASSWORD_SHA256`: hash SHA-256 hexadecimal da senha, não a senha em texto puro.
- `COOKIE_SECURE=1` em produção HTTPS.

Gere o hash da senha com Python no computador:

```bash
python -c "import getpass,hashlib; print(hashlib.sha256(getpass.getpass('Senha: ').encode()).hexdigest())"
```

## Publicação no Render
1. Crie um PostgreSQL para o LeadPilot AI.
2. Crie um Web Service ligado exclusivamente a `gtaocultoficial-sudo/leadpilot-ai`, nunca ao repositório `gta-oculto-ai`.
3. Build: `pip install -r requirements.txt`
4. Start: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120`
5. Configure as variáveis acima antes do deploy. Use a URL interna do banco PostgreSQL quando o banco e o serviço estiverem no Render.

## Limites conhecidos
Esta versão ainda não pesquisa empresas automaticamente, não usa API de IA externa e não envia mensagens. A pontuação é heurística e os textos gerados precisam de revisão. Próximos passos: conectar fontes empresariais autorizadas, deduplicar resultados, qualificar oportunidades e só então avaliar integrações de contato com limites, opt-out e respeito à LGPD.
