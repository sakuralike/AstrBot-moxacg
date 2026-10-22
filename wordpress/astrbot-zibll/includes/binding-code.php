<?php

namespace AstrBotZibll;

defined('ABSPATH') || exit;

function binding_session_table()
{
    global $wpdb;
    return $wpdb->prefix . 'astrbot_zibll_binding_sessions';
}

function binding_code_table()
{
    global $wpdb;
    return $wpdb->prefix . 'astrbot_zibll_binding_codes';
}

function install_binding_codes($collate)
{
    $sessions = binding_session_table();
    dbDelta("CREATE TABLE $sessions (
        session_id char(64) NOT NULL,
        actor_hash char(64) NOT NULL,
        active_actor char(64) DEFAULT NULL,
        start_key char(64) NOT NULL,
        start_input char(64) NOT NULL,
        state varchar(16) NOT NULL,
        attempts int(10) unsigned NOT NULL DEFAULT 0,
        created_at bigint(20) unsigned NOT NULL,
        expires_at bigint(20) unsigned NOT NULL,
        cleanup_at bigint(20) unsigned NOT NULL,
        user_id bigint(20) unsigned DEFAULT NULL,
        redeem_key char(64) DEFAULT NULL,
        PRIMARY KEY  (session_id),
        UNIQUE KEY active_actor (active_actor),
        UNIQUE KEY start_key (start_key),
        KEY actor_hash (actor_hash),
        KEY cleanup_at (cleanup_at),
        KEY user_id (user_id)
    ) ENGINE=InnoDB $collate;");
    $codes = binding_code_table();
    dbDelta("CREATE TABLE $codes (
        code_hash char(64) NOT NULL,
        session_id char(64) NOT NULL,
        user_id bigint(20) unsigned NOT NULL,
        active_candidate char(64) DEFAULT NULL,
        state varchar(16) NOT NULL,
        expires_at bigint(20) unsigned NOT NULL,
        cleanup_at bigint(20) unsigned NOT NULL,
        PRIMARY KEY  (code_hash),
        UNIQUE KEY active_candidate (active_candidate),
        KEY session_id (session_id),
        KEY user_id (user_id),
        KEY cleanup_at (cleanup_at)
    ) ENGINE=InnoDB $collate;");
}

function binding_schema_ready()
{
    global $wpdb;
    foreach (array(
        binding_session_table() => 'start_input', binding_code_table() => 'active_candidate',
        state_table() => 'payload', binding_table() => 'bound_at',
    ) as $table => $column) {
        if (!$wpdb->get_var($wpdb->prepare("SHOW COLUMNS FROM $table LIKE %s", $column))) {
            return false;
        }
    }
    return true;
}

function binding_transactions_ready()
{
    global $wpdb;
    foreach (array(binding_table(), binding_session_table(), binding_code_table(), state_table()) as $table) {
        $engine = $wpdb->get_var($wpdb->prepare(
            'SELECT ENGINE FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s', $table
        ));
        if (strtolower((string) $engine) !== 'innodb') {
            return false;
        }
    }
    return true;
}

function binding_transaction($callback)
{
    global $wpdb;
    if (!binding_transactions_ready() || $wpdb->query('START TRANSACTION') === false) {
        return failure('unavailable', 503);
    }
    $previous = $wpdb->suppress_errors(true);
    try {
        $result = $callback();
        if (is_wp_error($result)) {
            $wpdb->query('ROLLBACK');
            return $result;
        }
        if ($wpdb->query('COMMIT') === false) {
            $wpdb->query('ROLLBACK');
            return failure('unavailable', 503);
        }
        return $result;
    } catch (\Throwable $error) {
        $wpdb->query('ROLLBACK');
        return failure('unavailable', 503);
    } finally {
        $wpdb->suppress_errors($previous);
    }
}

function binding_write($result)
{
    if ($result === false) {
        throw new \RuntimeException('Binding storage unavailable.');
    }
    return $result;
}

function code_digest($code)
{
    return hash_hmac('sha256', $code, hash_hmac('sha256', 'astrbot-zibll:binding-code', wp_salt('auth')));
}

function code_session($session_id)
{
    global $wpdb;
    return $wpdb->get_row($wpdb->prepare(
        'SELECT * FROM ' . binding_session_table() . ' WHERE session_id = %s', $session_id
    ), ARRAY_A);
}

function cancel_actor_codes($actor_hash)
{
    global $wpdb;
    $sessions = binding_session_table();
    $codes = binding_code_table();
    binding_write($wpdb->query($wpdb->prepare(
        "UPDATE $codes c INNER JOIN $sessions s ON c.session_id = s.session_id
         SET c.state = 'invalidated', c.active_candidate = NULL
         WHERE s.actor_hash = %s AND c.state = 'active'", $actor_hash
    )));
    binding_write($wpdb->query($wpdb->prepare(
        "UPDATE $sessions SET state = 'cancelled', active_actor = NULL WHERE actor_hash = %s", $actor_hash
    )));
}

function cleanup_binding_codes()
{
    global $wpdb;
    foreach (array(binding_code_table(), binding_session_table()) as $table) {
        $wpdb->query($wpdb->prepare("DELETE FROM $table WHERE cleanup_at < %d LIMIT 100", time()));
    }
}

function binding_event_key($actor_hash, $action, $request_id)
{
    return hash('sha256', implode(':', array('binding-code', $actor_hash, $action, $request_id)));
}

function binding_code_action($action, $actor, $input)
{
    $request_id = $input['request_id'] ?? '';
    if (!is_string($request_id) || !preg_match('/\A[0-9a-f]{64}\z/', $request_id)) {
        return failure('event_identity_required', 400);
    }
    if ($action !== 'binding/code/start'
        && (!is_string($input['session_id'] ?? null) || !preg_match('/\A[0-9a-f]{64}\z/', $input['session_id']))
    ) {
        return failure('invalid_parameters', 400);
    }
    if ($action !== 'binding/code/status'
        && (!is_int($input['event_time'] ?? null) || abs(time() - $input['event_time']) > CLOCK_WINDOW)
    ) {
        return failure('event_expired', 400);
    }
    if ($action === 'binding/code/redeem'
        && (!is_string($input['code'] ?? null) || !preg_match('/\A[0-9]{6}\z/', $input['code']))
    ) {
        return failure('invalid_parameters', 400);
    }
    cleanup_binding_codes();
    return with_lock('actor:' . $actor['actor_hash'], static function () use ($action, $actor, $input) {
        return binding_transaction(static function () use ($action, $actor, $input) {
            if ($action === 'binding/code/start') {
                return start_binding_code($actor, $input);
            }
            return redeem_binding_code($actor, $input, $action === 'binding/code/status');
        });
    });
}

function current_code_binding($session)
{
    $row = binding_row($session['actor_hash']);
    if ($session['state'] !== 'bound' || !$row || !$row['user_id']
        || (int) $row['user_id'] !== (int) $session['user_id']
    ) {
        return success(array('state' => 'revoked'));
    }
    $user = available_user((int) $row['user_id']);
    return is_wp_error($user) ? $user : success(array(
        'state' => 'bound', 'uid' => (int) $user->ID, 'nickname' => $user->display_name,
    ));
}

function start_binding_code($actor, $input)
{
    global $wpdb;
    $key = binding_event_key($actor['actor_hash'], 'start', $input['request_id']);
    $fingerprint = hash('sha256', (string) $input['event_time']);
    $sessions = binding_session_table();
    $previous = $wpdb->get_row($wpdb->prepare("SELECT * FROM $sessions WHERE start_key = %s", $key), ARRAY_A);
    if ($previous) {
        if (!hash_equals($previous['start_input'], $fingerprint)) {
            return failure('request_conflict', 409);
        }
        return success(array('state' => 'already_started'));
    }
    $row = binding_row($actor['actor_hash']);
    if ($row && $row['user_id']) {
        $user = available_user((int) $row['user_id']);
        return is_wp_error($user) ? $user : success(array(
            'state' => 'bound', 'uid' => (int) $user->ID, 'nickname' => $user->display_name,
        ));
    }
    $limit = rate_limit('bind:' . $actor['actor_hash'], 3, 600);
    if (is_wp_error($limit)) {
        return $limit;
    }
    cancel_actor_codes($actor['actor_hash']);
    $token = bin2hex(random_bytes(32));
    $id = hash('sha256', $token);
    $now = time();
    $values = array_merge($actor, array(
        'candidate_id' => null, 'challenge_hash' => null, 'expires_at' => $now + 600, 'revoke_until' => 0,
    ));
    binding_write($row ? $wpdb->update(binding_table(), $values, array('actor_hash' => $actor['actor_hash']))
        : $wpdb->insert(binding_table(), $values));
    binding_write($wpdb->insert($sessions, array(
        'session_id' => $id, 'actor_hash' => $actor['actor_hash'], 'active_actor' => $actor['actor_hash'],
        'start_key' => $key, 'start_input' => $fingerprint, 'state' => 'pending',
        'created_at' => $now, 'expires_at' => $now + 600, 'cleanup_at' => $now + 600 + 86400,
    )));
    return success(array(
        'state' => 'pending', 'session_id' => $id, 'expires_in' => 600,
        'url' => add_query_arg('az_bind', $token, home_url('/')),
    ));
}

function redeem_binding_code($actor, $input, $status_only)
{
    global $wpdb;
    $session = code_session($input['session_id']);
    if (!$session || $session['actor_hash'] !== $actor['actor_hash']) {
        return failure('session_expired', 409);
    }
    $key = binding_event_key($actor['actor_hash'], 'redeem', $input['request_id']);
    $record = $wpdb->get_var($wpdb->prepare('SELECT payload FROM ' . state_table() . ' WHERE state_key = %s', $key));
    $record = $record ? json_decode($record, true) : null;
    $fingerprint = $status_only ? null : hash('sha256', implode(':', array(
        $input['session_id'], code_digest($input['code']), $input['event_time'],
    )));
    if ($record) {
        if ($record['session_id'] !== $input['session_id']
            || (!$status_only && !hash_equals($record['input_hash'], $fingerprint))
        ) {
            return failure('request_conflict', 409);
        }
        return $record['result'] === 'bound' ? current_code_binding($session)
            : success(array('state' => $record['result']));
    }
    if ($status_only) {
        return success(array('state' => 'not_processed'));
    }
    if ($session['state'] === 'locked') {
        return failure('too_many_attempts', 429);
    }
    if ($session['state'] !== 'pending' || (int) $session['expires_at'] <= time()) {
        return failure('session_expired', 409);
    }
    if ($input['event_time'] < (int) $session['created_at']) {
        return failure('event_expired', 400);
    }
    $codes = binding_code_table();
    $digest = code_digest($input['code']);
    $code = $wpdb->get_row($wpdb->prepare(
        "SELECT * FROM $codes WHERE code_hash = %s AND session_id = %s AND state = 'active' AND expires_at > %d",
        $digest, $input['session_id'], time()
    ), ARRAY_A);
    if (!$code) {
        $attempts = (int) $session['attempts'] + 1;
        $result = $attempts >= 5 ? 'too_many_attempts' : 'code_invalid';
        binding_write($wpdb->update(binding_session_table(), array(
            'attempts' => $attempts, 'state' => $attempts >= 5 ? 'locked' : 'pending',
        ), array('session_id' => $session['session_id'])));
    } else {
        $user = available_user((int) $code['user_id']);
        if (is_wp_error($user)) {
            return $user;
        }
        $row = binding_row($actor['actor_hash']);
        if (!$row || $row['user_id']) {
            return failure('binding_conflict', 409);
        }
        $saved = $wpdb->update(binding_table(), array(
            'user_id' => (int) $user->ID, 'bound_at' => time(), 'expires_at' => 0,
            'challenge_hash' => null, 'candidate_id' => null, 'revoke_until' => 0,
        ), array('actor_hash' => $actor['actor_hash'], 'user_id' => null));
        if ($saved !== 1) {
            $exists = $wpdb->get_var($wpdb->prepare(
                'SELECT actor_hash FROM ' . binding_table() . ' WHERE scope_hash = %s AND user_id = %d FOR UPDATE',
                $actor['scope_hash'], (int) $user->ID
            ));
            return failure($exists ? 'binding_conflict' : 'unavailable', $exists ? 409 : 503);
        }
        binding_write($wpdb->query($wpdb->prepare(
            "UPDATE $codes SET state = 'invalidated', active_candidate = NULL WHERE session_id = %s AND state = 'active'",
            $session['session_id']
        )));
        binding_write($wpdb->update($codes, array('state' => 'consumed'), array('code_hash' => $digest)));
        binding_write($wpdb->update(binding_session_table(), array(
            'state' => 'bound', 'active_actor' => null, 'user_id' => (int) $user->ID,
            'redeem_key' => $key, 'cleanup_at' => time() + 7 * 86400,
        ), array('session_id' => $session['session_id'])));
        $result = 'bound';
    }
    binding_write($wpdb->insert(state_table(), array(
        'state_key' => $key, 'expires_at' => time() + 7 * 86400,
        'payload' => wp_json_encode(array(
            'session_id' => $session['session_id'], 'input_hash' => $fingerprint, 'result' => $result,
        )),
    )));
    return $result === 'bound' ? current_code_binding(code_session($session['session_id']))
        : success(array('state' => $result));
}

function browser_issue_code($token, $input)
{
    if (!is_user_logged_in()) {
        return failure('authentication_failed', 401);
    }
    $uid = get_current_user_id();
    if (!is_string($token) || !preg_match('/\A[0-9a-f]{64}\z/', $token)
        || !is_string($input['_wpnonce'] ?? null) || !wp_verify_nonce($input['_wpnonce'], 'az_code_' . $token)
        || array_diff(array_keys($input), array('_wpnonce', 'az_action', 'regenerate', 'issue_key'))
        || ($input['az_action'] ?? '') !== 'issue_code'
        || !is_string($input['issue_key'] ?? null) || !preg_match('/\A[0-9a-f]{32}\z/', $input['issue_key'])
        || !in_array($input['regenerate'] ?? '0', array('0', '1'), true)
    ) {
        return failure('invalid_parameters', 400);
    }
    if (!transport_allowed(settings()) || settings()['enabled'] !== true || !theme_status()['ready']) {
        return failure('unavailable', 503);
    }
    $session = code_session(hash('sha256', $token));
    if (!$session) {
        return failure('session_expired', 409);
    }
    return with_lock('actor:' . $session['actor_hash'], static function () use ($session, $uid, $input) {
        return binding_transaction(static function () use ($session, $uid, $input) {
            global $wpdb;
            $key = hash('sha256', 'issue-request:' . $session['session_id'] . ':' . $uid . ':' . $input['issue_key']);
            if ($wpdb->get_var($wpdb->prepare('SELECT state_key FROM ' . state_table() . ' WHERE state_key = %s', $key))) {
                return success(array('state' => 'code_exists'));
            }
            $result = issue_binding_code($session['session_id'], $uid, ($input['regenerate'] ?? '0') === '1');
            if (!is_wp_error($result)) {
                binding_write($wpdb->insert(state_table(), array(
                    'state_key' => $key, 'expires_at' => (int) $session['expires_at'] + CLOCK_WINDOW,
                )));
            }
            return $result;
        });
    });
}

function issue_binding_code($session_id, $uid, $regenerate)
{
    global $wpdb;
    $session = code_session($session_id);
    if (!$session || $session['state'] !== 'pending' || (int) $session['expires_at'] <= time()) {
        return failure($session && $session['state'] === 'locked' ? 'too_many_attempts' : 'session_expired', 409);
    }
    $row = binding_row($session['actor_hash']);
    $identity = $row ? json_decode($row['identity'], true) : array();
    $scope = implode('|', array($identity['platform_id'] ?? '', $identity['bot_self_id'] ?? '', $identity['group_id'] ?? ''));
    if (!$row || $row['user_id'] || !in_array($scope, settings()['allowed_groups'], true)) {
        return failure('session_expired', 409);
    }
    $user = available_user($uid);
    if (is_wp_error($user)) {
        return $user;
    }
    $codes = binding_code_table();
    $candidate = hash('sha256', $session_id . ':' . $uid);
    $current = $wpdb->get_row($wpdb->prepare("SELECT * FROM $codes WHERE active_candidate = %s", $candidate), ARRAY_A);
    if ($current && !$regenerate) {
        return success(array('state' => 'code_exists'));
    }
    if (!$regenerate && $wpdb->get_var($wpdb->prepare(
        "SELECT code_hash FROM $codes WHERE session_id = %s AND user_id = %d LIMIT 1", $session_id, $uid
    ))) {
        return success(array('state' => 'code_exists'));
    }
    $count = (int) $wpdb->get_var($wpdb->prepare(
        "SELECT COUNT(*) FROM $codes WHERE session_id = %s AND state = 'active' AND expires_at > %d AND user_id <> %d",
        $session_id, time(), $uid
    ));
    if ($count >= 5) {
        return failure('rate_limited', 429);
    }
    foreach (array('issue:' . $candidate => 3, 'issue-user:' . $uid => 10) as $key => $limit) {
        $limited = rate_limit($key, $limit, 300);
        if (is_wp_error($limited)) {
            return $limited;
        }
    }
    binding_write($wpdb->query($wpdb->prepare(
        "UPDATE $codes SET state = 'invalidated', active_candidate = NULL WHERE active_candidate = %s", $candidate
    )));
    $expires = min(time() + 300, (int) $session['expires_at']);
    for ($attempt = 0; $attempt < 8; ++$attempt) {
        $plain = str_pad((string) random_int(0, 999999), 6, '0', STR_PAD_LEFT);
        $digest = code_digest($plain);
        $saved = $wpdb->insert($codes, array(
            'code_hash' => $digest, 'session_id' => $session_id, 'user_id' => $uid,
            'active_candidate' => $candidate, 'state' => 'active',
            'expires_at' => $expires, 'cleanup_at' => (int) $session['expires_at'] + CLOCK_WINDOW,
        ));
        if ($saved === 1) {
            return success(array('state' => 'issued', 'code' => $plain, 'expires_in' => $expires - time()));
        }
        if (!$wpdb->get_var($wpdb->prepare("SELECT code_hash FROM $codes WHERE code_hash = %s", $digest))) {
            return failure('unavailable', 503);
        }
    }
    return failure('busy', 409);
}

function remove_user_code_bindings($uid)
{
    global $wpdb;
    $actors = $wpdb->get_col($wpdb->prepare(
        'SELECT actor_hash FROM ' . binding_table() . ' WHERE user_id = %d OR candidate_id = %d
         UNION SELECT s.actor_hash FROM ' . binding_session_table() . ' s
         LEFT JOIN ' . binding_code_table() . ' c ON c.session_id = s.session_id
         WHERE s.user_id = %d OR c.user_id = %d', $uid, $uid, $uid, $uid
    ));
    foreach ($actors as $hash) {
        with_lock('actor:' . $hash, static function () use ($hash, $uid) {
            return binding_transaction(static function () use ($hash, $uid) {
                global $wpdb;
                $row = binding_row($hash);
                if ($row && ((int) $row['user_id'] === $uid || (int) $row['candidate_id'] === $uid)) {
                    clear_binding($hash);
                }
                binding_write($wpdb->query($wpdb->prepare(
                    'UPDATE ' . binding_code_table() . ' c INNER JOIN ' . binding_session_table() . " s
                     ON c.session_id = s.session_id SET c.state = 'invalidated', c.active_candidate = NULL
                     WHERE s.actor_hash = %s AND c.user_id = %d", $hash, $uid
                )));
                binding_write($wpdb->query($wpdb->prepare(
                    'UPDATE ' . binding_session_table() . " SET state = 'cancelled', active_actor = NULL, user_id = NULL
                     WHERE actor_hash = %s AND user_id = %d", $hash, $uid
                )));
                return true;
            });
        });
    }
}

function render_code_session($session, $token, $url, $result, $nonce)
{
    global $wpdb;
    $session = code_session($session['session_id']);
    if ($session['state'] !== 'pending' || (int) $session['expires_at'] <= time()) {
        echo '<p class="az-notice">此会话已结束、锁定或过期；请查看本人已有授权，或回群重新发起绑定。</p>';
        return;
    }
    $row = binding_row($session['actor_hash']);
    $identity = $row ? json_decode($row['identity'], true) : array();
    echo '<section><h2 class="az-section-title">核对本次群聊身份</h2><dl class="az-identity">';
    foreach (array('platform_id' => '平台实例', 'bot_self_id' => '机器人', 'group_id' => '群', 'sender_id' => '成员') as $key => $label) {
        echo '<dt>' . esc_html($label) . '</dt><dd>' . esc_html($identity[$key] ?? '') . '</dd>';
    }
    echo '</dl><p class="az-note">将此码由本人发送到发起绑定的原群，即同意展示头像、昵称、加入网站天数、等级、UID、积分、文章数和评论数，并允许本人网站签到。打开页面或生成码不会完成绑定。不要使用他人提供的码。</p>';
    $data = $result && !is_wp_error($result) ? $result->get_data()['data'] : array();
    $issued = ($data['state'] ?? '') === 'issued';
    $existed = (bool) $wpdb->get_var($wpdb->prepare(
        'SELECT code_hash FROM ' . binding_code_table() . ' WHERE session_id = %s AND user_id = %d LIMIT 1',
        $session['session_id'], get_current_user_id()
    ));
    echo '<label class="az-caption" for="az-code">本次六位授权码</label>';
    echo '<input id="az-code" class="az-code" readonly autocomplete="off" aria-describedby="az-code-time" value="'
        . esc_attr($issued ? $data['code'] : '') . '" placeholder="等待生成">';
    echo '<p id="az-code-time" class="az-after-action" data-seconds="' . ($issued ? (int) $data['expires_in'] : 0) . '">';
    echo esc_html($issued ? '授权码最多有效五分钟，请回原群 @机器人 发送。'
        : ($existed ? '已有发码记录；刷新不会恢复明文码。未保存或已过期时请明确重新生成，旧码将失效。' : '登录后自动生成一次授权码；也可手动点击生成。'));
    echo '</p><button id="az-copy" class="az-button az-button-secondary" type="button"'
        . ($issued ? '' : ' hidden') . '>复制授权码</button>';
    echo '<form id="az-code-form" method="post" action="' . esc_url($url) . '" data-auto="'
        . (!$existed && $result === null ? '1' : '0') . '">';
    wp_nonce_field('az_code_' . $token, '_wpnonce', false);
    echo '<input type="hidden" name="issue_key" value="' . esc_attr(bin2hex(random_bytes(16))) . '">';
    echo '<input type="hidden" name="az_action" value="issue_code"><input type="hidden" name="regenerate" value="'
        . ($existed ? '1' : '0') . '"><button class="az-button az-button-primary" type="submit">'
        . ($existed ? '重新生成（旧码失效）' : '生成授权码') . '</button></form>';
    echo '<noscript><p class="az-note">脚本未启用，请手动生成并复制；过期时间以网站校验为准。</p></noscript>';
    echo '<script nonce="' . esc_attr($nonce) . '">';
    readfile(dirname(__DIR__) . '/assets/binding-code.js');
    echo '</script></section>';
}
