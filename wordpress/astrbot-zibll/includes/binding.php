<?php

namespace AstrBotZibll;

defined('ABSPATH') || exit;

function binding_table()
{
    global $wpdb;
    return $wpdb->prefix . 'astrbot_zibll_bindings';
}

function success($data)
{
    return new \WP_REST_Response(array(
        'request_id' => wp_generate_uuid4(), 'code' => 'ok',
        'message' => 'OK', 'data' => $data,
    ));
}

function actor_data($request)
{
    $input = $request->get_json_params();
    $actor = $input['actor'] ?? null;
    $keys = array('platform_id', 'bot_self_id', 'group_id', 'sender_id');
    if (!is_array($actor) || count($actor) !== 4 || array_diff($keys, array_keys($actor))) {
        return failure('invalid_parameters', 400);
    }
    $fields = array();
    foreach ($keys as $key) {
        if (!is_string($actor[$key]) || preg_match('/\A[^\s\p{C}|]{1,128}\z/u', $actor[$key]) !== 1) {
            return failure('invalid_parameters', 400);
        }
        $fields[] = $actor[$key];
    }
    $options = settings();
    if (!in_array(implode('|', array_slice($fields, 0, 3)), $options['allowed_groups'], true)
        || $actor['sender_id'] === $actor['bot_self_id']
    ) {
        return failure('forbidden', 403);
    }
    $scope = array_merge(array($options['client_id']), array_slice($fields, 0, 3));
    return array(
        'actor_hash' => hash('sha256', wp_json_encode(array_merge($scope, array($fields[3])))),
        'scope_hash' => hash('sha256', wp_json_encode($scope)),
        'identity' => wp_json_encode($actor, JSON_UNESCAPED_UNICODE),
    );
}

function with_lock($key, $callback)
{
    global $wpdb;
    $name = 'az_' . substr(hash('sha256', DB_NAME . ':' . $wpdb->prefix . ':' . $key), 0, 60);
    if ((string) $wpdb->get_var($wpdb->prepare('SELECT GET_LOCK(%s, 3)', $name)) !== '1') {
        return failure('busy', 409);
    }
    try {
        return $callback();
    } finally {
        $wpdb->get_var($wpdb->prepare('SELECT RELEASE_LOCK(%s)', $name));
    }
}

function rate_limit($key, $limit, $window)
{
    global $wpdb;
    $table = state_table();
    $bucket = (int) floor(time() / $window);
    $digest = hash('sha256', 'limit:' . $key . ':' . $bucket);
    $result = $wpdb->query($wpdb->prepare(
        "INSERT INTO $table (state_key, expires_at, hits) VALUES (%s, %d, 1)
         ON DUPLICATE KEY UPDATE hits = hits + 1",
        $digest, ($bucket + 1) * $window
    ));
    if ($result === false) {
        return failure('unavailable', 503);
    }
    $count = $wpdb->get_var($wpdb->prepare("SELECT hits FROM $table WHERE state_key = %s", $digest));
    return (int) $count <= $limit ? true : failure('rate_limited', 429);
}

function binding_row($actor_hash)
{
    global $wpdb;
    return $wpdb->get_row($wpdb->prepare(
        'SELECT * FROM ' . binding_table() . ' WHERE actor_hash = %s', $actor_hash
    ), ARRAY_A);
}

function available_user($uid)
{
    $user = get_userdata($uid);
    if (!$user) {
        return failure('account_unavailable', 404);
    }
    $ban = (int) get_user_meta($uid, 'banned', true);
    $until = get_user_meta($uid, 'banned_time', true);
    if (_pz('user_ban_s', true) && $ban
        && (!$until || $until >= current_time('mysql'))
    ) {
        return failure('account_restricted', 403);
    }
    return $user;
}

function bound_user($actor_hash)
{
    $row = binding_row($actor_hash);
    return $row && $row['user_id'] ? available_user((int) $row['user_id']) : failure('not_bound', 404);
}

function binding_action($action, $actor, $input)
{
    global $wpdb;
    $hash = $actor['actor_hash'];
    return with_lock('actor:' . $hash, static function () use ($action, $actor, $input, $hash, $wpdb) {
        $table = binding_table();
        $row = binding_row($hash);
        if ($action === 'binding/start') {
            if ($row && $row['user_id']) {
                return failure('already_bound', 409);
            }
            $limit = rate_limit('bind:' . $hash, 3, 600);
            if (is_wp_error($limit)) {
                return $limit;
            }
            $wpdb->query($wpdb->prepare(
                "DELETE FROM $table WHERE user_id IS NULL AND expires_at < %d AND actor_hash <> %s LIMIT 100",
                time(), $hash
            ));
            $token = bin2hex(random_bytes(32));
            $values = array_merge($actor, array(
                'challenge_hash' => hash('sha256', $token), 'candidate_id' => null,
                'expires_at' => time() + 600, 'revoke_until' => 0,
            ));
            $saved = $row
                ? $wpdb->update($table, $values, array('actor_hash' => $hash))
                : $wpdb->insert($table, $values);
            if ($saved === false) {
                return failure('unavailable', 503);
            }
            return success(array(
                'url' => add_query_arg(array('az_bind' => $token), home_url('/')),
                'expires_in' => 600,
            ));
        }
        if ($action === 'binding/revoke') {
            if (!$row || !$row['user_id']) {
                return failure('not_bound', 404);
            }
            if (($input['confirm'] ?? false) !== true) {
                if ($wpdb->update($table, array('revoke_until' => time() + 120), array('actor_hash' => $hash)) === false) {
                    return failure('unavailable', 503);
                }
                return success(array('state' => 'confirm_required', 'uid' => (int) $row['user_id']));
            }
            if ((int) $row['revoke_until'] < time()) {
                return failure('confirmation_expired', 409);
            }
            return revoke_binding($hash, (int) $row['user_id']);
        }
        if ($row && $row['user_id']) {
            $user = available_user((int) $row['user_id']);
            return is_wp_error($user) ? $user : success(array(
                'state' => 'bound', 'uid' => (int) $user->ID, 'nickname' => $user->display_name,
            ));
        }
        if (!$row || !$row['challenge_hash'] || (int) $row['expires_at'] < time()) {
            return failure('challenge_expired', 409);
        }
        if (!$row['candidate_id']) {
            return $action === 'binding/status'
                ? success(array('state' => 'awaiting_website'))
                : failure('website_authorization_required', 409);
        }
        $user = available_user((int) $row['candidate_id']);
        if (is_wp_error($user)) {
            return $user;
        }
        if ($action === 'binding/status') {
            return success(array(
                'state' => 'awaiting_confirmation', 'uid' => (int) $user->ID,
                'nickname' => $user->display_name, 'challenge_id' => $row['challenge_hash'],
            ));
        }
        if (($input['expected_uid'] ?? null) !== (int) $user->ID
            || ($input['challenge_id'] ?? '') !== $row['challenge_hash']
        ) {
            return failure('candidate_mismatch', 409);
        }
        $previous = $wpdb->suppress_errors(true);
        $saved = $wpdb->update($table, array(
            'user_id' => (int) $user->ID, 'candidate_id' => null,
            'challenge_hash' => null, 'expires_at' => 0,
        ), array('actor_hash' => $hash));
        $wpdb->suppress_errors($previous);
        return $saved === 1
            ? success(array('state' => 'bound', 'uid' => (int) $user->ID, 'nickname' => $user->display_name))
            : failure('binding_conflict', 409);
    });
}

function revoke_binding($actor_hash, $uid)
{
    global $wpdb;
    $saved = $wpdb->update(binding_table(), array(
        'user_id' => null, 'candidate_id' => null, 'challenge_hash' => null,
        'expires_at' => 0, 'revoke_until' => 0,
    ), array('actor_hash' => $actor_hash, 'user_id' => $uid));
    return $saved === 1 ? success(array('state' => 'revoked')) : failure('binding_changed', 409);
}

function browser_authorize($token, $uid, $nonce)
{
    global $wpdb;
    if (!is_string($token) || !preg_match('/\A[0-9a-f]{64}\z/', $token)
        || !$uid || !is_string($nonce) || !wp_verify_nonce($nonce, 'az_bind_' . $token)
    ) {
        return failure('invalid_nonce', 403);
    }
    $digest = hash('sha256', $token);
    $row = $wpdb->get_row($wpdb->prepare(
        'SELECT * FROM ' . binding_table() . ' WHERE challenge_hash = %s', $digest
    ), ARRAY_A);
    if (!$row) {
        return failure('challenge_expired', 409);
    }
    return with_lock('actor:' . $row['actor_hash'], static function () use ($row, $digest, $uid, $wpdb) {
        $current = binding_row($row['actor_hash']);
        if (!$current || $current['challenge_hash'] !== $digest
            || (int) $current['expires_at'] < time() || $current['user_id']
        ) {
            return failure('challenge_expired', 409);
        }
        $actor = json_decode($current['identity'], true);
        $scope = implode('|', array($actor['platform_id'], $actor['bot_self_id'], $actor['group_id']));
        if (!in_array($scope, settings()['allowed_groups'], true)) {
            return failure('forbidden', 403);
        }
        $user = available_user($uid);
        if (is_wp_error($user)) {
            return $user;
        }
        if ($current['candidate_id'] && (int) $current['candidate_id'] !== $uid) {
            return failure('candidate_locked', 409);
        }
        $saved = $wpdb->update(binding_table(), array('candidate_id' => $uid), array('actor_hash' => $row['actor_hash']));
        return $saved === false ? failure('unavailable', 503) : success(array('state' => 'awaiting_confirmation'));
    });
}

function binding_page()
{
    global $wpdb;
    if (!isset($_GET['az_bind']) && !isset($_GET['az_bindings'])) {
        return;
    }
    if (!defined('DONOTCACHEPAGE')) {
        define('DONOTCACHEPAGE', true);
    }
    nocache_headers();
    header('Referrer-Policy: no-referrer');
    header('X-Frame-Options: DENY');
    header("Content-Security-Policy: default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'");
    if (!transport_allowed(settings())) {
        wp_die('请使用 HTTPS 访问。', '', array('response' => 403));
    }
    $token = isset($_GET['az_bind']) && is_string($_GET['az_bind']) ? wp_unslash($_GET['az_bind']) : '';
    if ($token && !preg_match('/\A[0-9a-f]{64}\z/', $token)) {
        wp_die('绑定链接无效。', '', array('response' => 400));
    }
    $url = $token ? add_query_arg('az_bind', $token, home_url('/')) : add_query_arg('az_bindings', '1', home_url('/'));
    if (!is_user_logged_in()) {
        wp_safe_redirect(wp_login_url($url));
        exit;
    }
    $uid = get_current_user_id();
    $result = null;
    if ($_SERVER['REQUEST_METHOD'] === 'POST') {
        $nonce = isset($_POST['_wpnonce']) && is_string($_POST['_wpnonce']) ? wp_unslash($_POST['_wpnonce']) : '';
        if ($token) {
            $result = settings()['enabled'] === true
                ? browser_authorize($token, $uid, $nonce) : failure('unavailable', 503);
        } else {
            $hash = $_POST['binding'] ?? '';
            if (!is_string($hash) || !preg_match('/\A[0-9a-f]{64}\z/', $hash)
                || !wp_verify_nonce($nonce, 'az_revoke_' . $hash)
            ) {
                $result = failure('invalid_nonce', 403);
            } else {
                $result = with_lock('actor:' . $hash, static fn() => revoke_binding($hash, $uid));
            }
        }
    }
    echo '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">';
    echo '<title>网站群聊授权</title><body><main><h1>网站群聊授权</h1>';
    if ($result !== null) {
        echo '<p>' . esc_html(is_wp_error($result)
            ? '操作失败：' . $result->get_error_code()
            : ($token ? '网站授权已记录，请回原群核对昵称和 UID 并完成绑定确认。' : '授权已撤销。')) . '</p>';
    }
    if ($token && settings()['enabled'] === true) {
        $row = $wpdb->get_row($wpdb->prepare(
            'SELECT * FROM ' . binding_table() . ' WHERE challenge_hash = %s AND expires_at >= %d',
            hash('sha256', $token), time()
        ), ARRAY_A);
        if ($row) {
            $identity = json_decode($row['identity'], true);
            echo '<p>仅当以下身份是你本人且由你发起绑定时授权：</p><dl>';
            foreach (array('platform_id' => '平台实例', 'bot_self_id' => '机器人', 'group_id' => '群', 'sender_id' => '成员') as $key => $label) {
                echo '<dt>' . esc_html($label) . '</dt><dd>' . esc_html($identity[$key]) . '</dd>';
            }
            echo '</dl><p>授权后，你在该群查询时将展示头像、昵称、加入网站天数、等级、UID、积分、已发布文章数及评论数，并可触发本人网站签到。</p>';
            echo '<p>当前网站 UID：' . (int) $uid . '；昵称：' . esc_html(wp_get_current_user()->display_name) . '</p>';
            echo '<form method="post" action="' . esc_url($url) . '">';
            wp_nonce_field('az_bind_' . $token, '_wpnonce', false);
            echo '<button type="submit">同意授权，回原群确认</button></form>';
        } else {
            echo '<p>链接已失效，请回群重新发起绑定。</p>';
        }
    }
    echo '<h2>我的已有授权</h2>';
    $rows = $wpdb->get_results($wpdb->prepare(
        'SELECT actor_hash, identity FROM ' . binding_table() . ' WHERE user_id = %d', $uid
    ), ARRAY_A);
    foreach ($rows as $row) {
        $identity = json_decode($row['identity'], true);
        echo '<form method="post" action="' . esc_url(add_query_arg('az_bindings', '1', home_url('/'))) . '"><p>';
        echo esc_html(implode(' / ', $identity)) . '</p>';
        wp_nonce_field('az_revoke_' . $row['actor_hash'], '_wpnonce', false);
        echo '<input type="hidden" name="binding" value="' . esc_attr($row['actor_hash']) . '">';
        echo '<button type="submit">撤销此群授权</button></form>';
    }
    echo '</main></body></html>';
    exit;
}
