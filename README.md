# LeadPilot AI — MVP protegido

Aplicação independente para organizar, prospectar e acompanhar oportunidades comerciais. Não faz parte do GTA Oculto AI.

# LeadPilot AI — Hub de afiliados e marketplaces

O objetivo principal do LeadPilot AI é encontrar oportunidades de produtos em marketplaces, organizar links oficiais de afiliados e preparar materiais de divulgação. O foco inicial é o Mercado Livre; Shopee, Amazon e outros marketplaces entram como conectores seguintes. O projeto é independente do GTA Oculto AI.

## Recursos do hub de marketplaces
- Pesquisa de anúncios públicos do Mercado Livre pela API pública do site brasileiro.
- Triagem inicial por condição do produto, frete, reputação do vendedor, desconto anunciado e unidades vendidas quando esses dados vêm na resposta.
- Lista persistente de produtos salvos no PostgreSQL, com preço consultado, anúncio e indicador heurístico de oportunidade.
- Campo para guardar o link oficial de afiliado gerado no portal de cada marketplace.
- Rascunho de texto promocional baseado no título e no preço consultado, para revisão antes de publicar.
- Acesso rápido aos programas oficiais de afiliados do Mercado Livre, Shopee e Amazon.
- Login obrigatório e rotas protegidas.

## Limites importantes
- O indicador de oportunidade é uma triagem simples; não é uma estimativa de comissão, probabilidade comprovada de venda ou garantia de renda.
- A busca automática de catálogo está implementada primeiro para o Mercado Livre. Shopee e Amazon ainda não têm conectores de pesquisa integrados.
- O LeadPilot não gera por conta própria um link de afiliado válido. É necessário estar aprovado no programa da plataforma, gerar o link oficial do produto e colá-lo no registro salvo.
- O programa do Mercado Livre pode restringir tipos de páginas e canais de divulgação. Use links de produtos específicos, gere-os pelas ferramentas oficiais e divulgue somente nos canais aceitos e cadastrados no programa.
- O aplicativo ainda não publica conteúdo nem mensagens automaticamente. Os textos são rascunhos para revisar; publicar e verificar resultados são etapas futuras.
- O módulo antigo de prospecção de empresas fica disponível como legado, mas deixou de ser o foco principal do projeto.
- As regras, disponibilidade e comissões dos programas podem mudar; confirme sempre as condições diretamente em cada portal oficial.

## Configuração obrigatória
O aplicativo deve falhar ao iniciar se as variáveis essenciais não estiverem definidas, evitando publicar uma versão sem autenticação ou banco.

- `DATABASE_URL`: URL de conexão do PostgreSQL.
- `SECRET_KEY`: segredo forte e aleatório para sessões Flask.
- `APP_USERNAME`: usuário de acesso.
- `APP_PASSWORD_SHA256`: hash SHA-256 hexadecimal da senha, não a senha em texto puro.
- `COOKIE_SECURE=1` em produção HTTPS.

Gere o hash da senha com Python:

```bash
python -c "import getpass,hashlib; print(hashlib.sha256(getpass.getpass('Senha: ').encode()).hexdigest())"
```

## Publicação no Render
1. Crie um PostgreSQL para o LeadPilot AI.
2. Crie um Web Service ligado exclusivamente a `gtaocultooficial-sudo/leadpilot-ai`, nunca ao repositório `gta-oculto-ai`.
3. Build: `pip install -r requirements.txt`
4. Start: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120`
5. Configure as variáveis acima antes do deploy. Use a URL interna do banco PostgreSQL quando o banco e o serviço estiverem no Render.

## Próximos passos recomendados
1. Integrar a pesquisa de catálogo da Shopee e da Amazon seguindo suas regras e mecanismos oficiais.
2. Criar um radar programado para detectar mudanças relevantes de preço, disponibilidade e ofertas sem exceder limites de API.
3. Gerar variações de textos e roteiros para Shorts/Reels, com revisão e histórico dos materiais.
4. Adicionar acompanhamento de links, cliques e conversões usando os recursos de relatórios permitidos por cada programa.
5. Testes automatizados para pesquisa, validação dos links, deduplicação e persistência de dados.
