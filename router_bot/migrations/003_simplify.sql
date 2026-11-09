-- Retire the username cache, keeping explicit contacts and IDs in loans/history.
DROP TABLE IF EXISTS telegram_users;
DELETE FROM settings WHERE key='colors_managed';
-- Old admin previews have no scoped cancel button. Require a fresh preview.
DELETE FROM drafts WHERE kind='confirm' AND json_extract(data,'$.data.syncDraft')
 IN (SELECT id FROM drafts WHERE kind='sync');
DELETE FROM drafts WHERE kind IN ('sync','colors','launch');
