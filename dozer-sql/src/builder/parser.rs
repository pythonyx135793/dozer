//! Keep Dozer's dialect while admitting a parenthesized query after IN.
use dozer_sql_expression::sqlparser::{
    ast::Statement,
    dialect::DozerDialect,
    keywords::Keyword,
    parser::{Parser, ParserError},
    tokenizer::{Token, Tokenizer},
};

pub(super) fn parse_sql(sql: &str) -> Result<Vec<Statement>, ParserError> {
    let dialect = DozerDialect {};
    let mut tokens = Tokenizer::new(&dialect, sql)
        .tokenize()?
        .into_iter()
        .filter(|token| !matches!(token, Token::Whitespace(_)))
        .collect::<Vec<_>>();
    let mut rewritten = false;
    // Work backwards so nested membership queries are handled first and edits
    // never change the indexes of the remaining IN tokens.
    for index in (0..tokens.len()).rev() {
        if !matches!(&tokens[index], Token::Word(word) if word.keyword == Keyword::IN)
            || tokens.get(index + 1) != Some(&Token::LParen)
            || tokens.get(index + 2) != Some(&Token::LParen)
        {
            continue;
        }
        let start = index + 2;
        let mut depth = 1;
        let end = (start..tokens.len()).find(|offset| {
            match tokens[*offset] {
                Token::LParen => depth += 1,
                Token::RParen => depth -= 1,
                _ => {}
            }
            depth == 0
        });
        let Some(end) = end else { continue };
        let Some(Token::Word(first)) = tokens[start..end]
            .iter()
            .find(|token| **token != Token::LParen)
        else {
            continue;
        };
        if !matches!(first.keyword, Keyword::SELECT | Keyword::WITH) {
            continue;
        }
        let body = tokens[start..end].to_vec();
        let mut query = Parser::new(&dialect).with_tokens(body.clone());
        if query.parse_query().is_err() || query.peek_token().token != Token::EOF {
            // A scalar expression/list or malformed query must keep the original
            // parser's behavior, including errors; never consume only a prefix.
            continue;
        }
        // The locked parser's parse_in dispatches only immediate SELECT/WITH,
        // although parse_query supports parentheses and parenthesized UNION.
        // A relational identity wrapper preserves the complete original query,
        // its grouping/CTEs, columns and multiplicity. Token handling leaves
        // quoted text untouched and keeps Dozer's typed-function syntax intact.
        let mut wrapped = vec![
            Token::make_keyword("SELECT"),
            Token::Mul,
            Token::make_keyword("FROM"),
            Token::LParen,
        ];
        wrapped.extend(body);
        wrapped.push(Token::RParen);
        tokens.splice(start..end, wrapped);
        rewritten = true;
    }
    if rewritten {
        Parser::new(&dialect).with_tokens(tokens).parse_statements()
    } else {
        // Retain the original tokenizer's locations and errors for unchanged SQL.
        Parser::parse_sql(&dialect, sql)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use dozer_sql_expression::sqlparser::ast::{Expr, SetExpr};

    #[test]
    fn ordinary_dozer_syntax_and_static_lists_are_unchanged() {
        for sql in [
            "SELECT key FROM rows WHERE key IN (1, NULL, (2 + 3))",
            "SELECT key FROM rows WHERE key NOT IN ((1), (2))",
            "SELECT 'IN ((SELECT key FROM rows))', \"IN\" FROM rows",
            "SELECT predict<int>(key), value[predict<int>(key)] FROM rows",
            "SELECT key FROM rows WHERE key IN ((SELECT key FROM members), 1)",
            "SELECT key FROM rows WHERE key IN ((SELECT key FROM members) + 1)",
        ] {
            assert_eq!(
                parse_sql(sql).unwrap(),
                Parser::parse_sql(&DozerDialect {}, sql).unwrap(),
                "{sql}"
            );
        }
    }

    #[test]
    fn parenthesized_queries_use_membership_including_not_and_comments() {
        for sql in [
            "SELECT key FROM rows WHERE key IN ((SELECT key FROM members))",
            "SELECT key FROM rows WHERE key NOT IN (/* query */ (((SELECT key FROM members))))",
            "SELECT key FROM rows WHERE key IN ((SELECT key FROM members) UNION ALL (SELECT key FROM others))",
            "SELECT key FROM rows WHERE key IN ((WITH c AS (SELECT key FROM members) SELECT key FROM c))",
        ] {
            let Statement::Query(query) = parse_sql(sql).unwrap().remove(0) else {
                panic!("expected query")
            };
            let SetExpr::Select(select) = *query.body else {
                panic!("expected SELECT")
            };
            assert!(matches!(select.selection, Some(Expr::InSubquery { .. })), "{sql}");
        }
    }

    #[test]
    fn malformed_parentheses_keep_the_original_parser_error() {
        for sql in [
            "SELECT key FROM rows WHERE key IN ((SELECT key FROM members)",
            "SELECT key FROM rows WHERE key IN ((SELECT key FROM))",
            "SELECT key FROM rows WHERE key IN ((SELECT key FROM members) UNION)",
        ] {
            assert_eq!(
                parse_sql(sql).unwrap_err(),
                Parser::parse_sql(&DozerDialect {}, sql).unwrap_err(),
                "{sql}"
            );
        }
    }
}
