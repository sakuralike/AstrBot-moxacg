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
            $cancelled = binding_transaction(static function () use ($hash) {
                cancel_actor_codes($hash);
                return true;
            });
            if (is_wp_error($cancelled)) {
                return $cancelled;
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
            'challenge_hash' => null, 'expires_at' => 0, 'bound_at' => time(),
        ), array('actor_hash' => $hash));
        $wpdb->suppress_errors($previous);
        return $saved === 1
            ? success(array('state' => 'bound', 'uid' => (int) $user->ID, 'nickname' => $user->display_name))
            : failure('binding_conflict', 409);
    });
}

function revoke_binding($actor_hash, $uid)
{
    return with_lock('actor:' . $actor_hash, static function () use ($actor_hash, $uid) {
        return binding_transaction(static function () use ($actor_hash, $uid) {
            $row = binding_row($actor_hash);
            if (!$row || !$row['user_id'] || (int) $row['user_id'] !== $uid) {
                return failure('binding_changed', 409);
            }
            clear_binding($actor_hash);
            return success(array('state' => 'revoked'));
        });
    });
}

function clear_binding($actor_hash)
{
    global $wpdb;
    cancel_actor_codes($actor_hash);
    binding_write($wpdb->update(binding_table(), array(
        'user_id' => null, 'candidate_id' => null, 'challenge_hash' => null,
        'expires_at' => 0, 'revoke_until' => 0,
    ), array('actor_hash' => $actor_hash)));
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
    $style_nonce = bin2hex(random_bytes(16));
    header("Content-Security-Policy: default-src 'none'; style-src 'nonce-$style_nonce'; script-src 'nonce-$style_nonce'; img-src https: http:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'");
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
    $code_session = $token ? code_session(hash('sha256', $token)) : null;
    $result = null;
    if ($_SERVER['REQUEST_METHOD'] === 'POST') {
        $nonce = isset($_POST['_wpnonce']) && is_string($_POST['_wpnonce']) ? wp_unslash($_POST['_wpnonce']) : '';
        if ($token && $code_session) {
            $result = browser_issue_code($token, wp_unslash($_POST));
        } elseif ($token) {
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
    $options = settings();
    $background_color = is_string($options['binding_background_color'])
        ? sanitize_hex_color($options['binding_background_color']) : null;
    $background_image = is_string($options['binding_background_image'])
        ? esc_url_raw($options['binding_background_image'], array('http', 'https')) : '';
    echo '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">';
    echo '<meta name="referrer" content="no-referrer">';
    echo '<title>网站群聊授权</title><style nonce="' . esc_attr($style_nonce) . '">';
    readfile(dirname(__DIR__) . '/assets/binding.css');
    echo ':root{--az-background:' . ($background_color ?: '#fff0f5') . ';}';
    echo '</style></head><body><div class="az-backdrop" aria-hidden="true">';
    if ($background_image !== '') {
        echo '<img id="az-background-image" src="' . esc_url($background_image) . '" alt="" referrerpolicy="no-referrer" hidden>';
        echo '<script nonce="' . esc_attr($style_nonce) . '">(() => {'
            . 'const image = document.getElementById("az-background-image");'
            . 'const reveal = () => { if (image.naturalWidth > 0) image.hidden = false; };'
            . 'image.addEventListener("load", reveal, { once: true }); reveal();'
            . '})();</script>';
    }
    echo '</div>';
    echo '<main class="az-shell"><div class="az-card"><header class="az-header">';
    echo '<div class="az-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
        . '<path d="M10 13a5 5 0 0 0 7.1 0l3-3a5 5 0 0 0-7.1-7.1l-1.7 1.7M14 11a5 5 0 0 0-7.1 0l-3 3a5 5 0 0 0 7.1 7.1l1.7-1.7"/>'
        . '</svg></div><p class="az-eyebrow">ASTRBOT &middot; ACCOUNT LINK</p><h1>网站群聊授权</h1>';
    echo '<p class="az-subtitle">连接网站账号，与你的群聊身份相遇。</p></header>';
    echo '<div class="az-account"><div><p class="az-caption">当前登录账号</p><strong>'
        . esc_html(wp_get_current_user()->display_name) . '</strong></div><span class="az-uid">UID '
        . (int) $uid . '</span></div>';
    if ($result !== null) {
        echo '<p class="az-notice' . (is_wp_error($result) ? ' az-notice-error' : '') . '" role="status">'
            . esc_html(is_wp_error($result)
            ? '操作失败：' . $result->get_error_code()
            : ($code_session ? '请核对当前账号，将授权码由本人发回原群。'
                : ($token ? '网站授权已记录，请回原群核对昵称和 UID 并完成绑定确认。' : '授权已撤销。'))) . '</p>';
    }
    $labels = array('platform_id' => '平台实例', 'bot_self_id' => '机器人', 'group_id' => '群', 'sender_id' => '成员');
    if ($code_session && $options['enabled'] === true) {
        render_code_session($code_session, $token, $url, $result, $style_nonce);
    } elseif ($token && $options['enabled'] === true) {
        $row = $wpdb->get_row($wpdb->prepare(
            'SELECT * FROM ' . binding_table() . ' WHERE challenge_hash = %s AND expires_at >= %d',
            hash('sha256', $token), time()
        ), ARRAY_A);
        if ($row) {
            $identity = json_decode($row['identity'], true);
            echo '<section aria-labelledby="az-request-title"><h2 class="az-section-title" id="az-request-title">确认群聊身份</h2>';
            echo '<p class="az-caption">仅当以下身份是你本人且由你发起绑定时授权。</p><dl class="az-identity">';
            foreach ($labels as $key => $label) {
                echo '<dt>' . esc_html($label) . '</dt><dd>' . esc_html($identity[$key]) . '</dd>';
            }
            echo '</dl><p class="az-note"><strong>授权范围</strong><br>授权后，你在该群查询时将展示头像、昵称、加入网站天数、等级、UID、积分、已发布文章数及评论数，并可触发本人网站签到。</p>';
            echo '<form method="post" action="' . esc_url($url) . '">';
            wp_nonce_field('az_bind_' . $token, '_wpnonce', false);
            echo '<button class="az-button az-button-primary" type="submit">同意授权，回原群确认 &rarr;</button></form>';
            echo '<p class="az-after-action">网站授权后，还需回到原群核对昵称与 UID，完成绑定。</p></section>';
        } else {
            echo '<p class="az-notice" role="status">链接已失效，请回群重新发起绑定。</p>';
        }
    } elseif ($token) {
        echo '<p class="az-notice" role="status">网站授权暂不可用，请联系管理员。</p>';
    } else {
        echo '<p class="az-note">如需新增授权，请先回群发送绑定指令，再打开机器人回复的本次授权链接。此管理页不生成通用授权码。</p>';
    }
    $rows = $wpdb->get_results($wpdb->prepare(
        'SELECT actor_hash, identity FROM ' . binding_table() . ' WHERE user_id = %d', $uid
    ), ARRAY_A);
    echo '<section class="az-existing" aria-labelledby="az-existing-title"><div class="az-existing-heading">';
    echo '<h2 class="az-section-title" id="az-existing-title">我的已有授权</h2><span class="az-count">'
        . count($rows) . ' 个群聊</span></div>';
    if (!$rows) {
        echo '<p class="az-empty">还没有已绑定的群聊。<br>'
            . ($code_session ? '核对账号后，回原群 @机器人 发送六位授权码。' : '请先回群发起绑定，再按授权页说明操作。') . '</p>';
    }
    foreach ($rows as $row) {
        $identity = json_decode($row['identity'], true);
        echo '<form class="az-binding" method="post" action="' . esc_url(add_query_arg('az_bindings', '1', home_url('/'))) . '"><dl class="az-identity">';
        foreach ($labels as $key => $label) {
            echo '<dt>' . esc_html($label) . '</dt><dd>' . esc_html($identity[$key]) . '</dd>';
        }
        echo '</dl>';
        wp_nonce_field('az_revoke_' . $row['actor_hash'], '_wpnonce', false);
        echo '<input type="hidden" name="binding" value="' . esc_attr($row['actor_hash']) . '">';
        echo '<button class="az-button az-button-secondary" type="submit">撤销此群授权</button></form>';
    }
    echo '</section><footer class="az-footer"><a href="' . esc_url(home_url('/')) . '">返回网站</a></footer>';
    echo '</div></main></body></html>';
    exit;
}
