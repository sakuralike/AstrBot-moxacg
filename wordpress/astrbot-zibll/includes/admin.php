<?php

namespace AstrBotZibll;

defined('ABSPATH') || exit;

function admin_menu()
{
    $hook = add_options_page(
        'AstrBot 网站联动', 'AstrBot 网站联动', 'manage_options',
        'astrbot-zibll', __NAMESPACE__ . '\\admin_page'
    );
    add_action('load-' . $hook, __NAMESPACE__ . '\\admin_headers');
}

function admin_headers()
{
    if (!current_user_can('manage_options')) {
        wp_die(esc_html('无权访问此设置页。'));
    }
    nocache_headers();
    header('Referrer-Policy: no-referrer');
}

function parse_groups($text)
{
    if (!is_string($text) || strlen($text) > 40000) {
        return failure('invalid_groups', 400);
    }
    $groups = preg_split('/\r\n|\r|\n/', trim($text, "\r\n"), -1, PREG_SPLIT_NO_EMPTY);
    if (count($groups) > 100) {
        return failure('invalid_groups', 400);
    }
    foreach ($groups as $group) {
        $fields = explode('|', $group);
        if (count($fields) !== 3) {
            return failure('invalid_groups', 400);
        }
        foreach ($fields as $field) {
            if (preg_match('/\A[^\s\p{C}|]{1,128}\z/u', $field) !== 1) {
                return failure('invalid_groups', 400);
            }
        }
    }
    return array_values(array_unique($groups));
}

function save_settings($input)
{
    if (!current_user_can('manage_options')) {
        return failure('forbidden', 403);
    }
    $nonce = $input['_wpnonce'] ?? '';
    if (!is_string($nonce) || !wp_verify_nonce($nonce, 'astrbot_zibll_settings')) {
        return failure('invalid_nonce', 403);
    }
    $action = $input['az_action'] ?? '';
    if (!in_array($action, array('save', 'rotate', 'revoke'), true)) {
        return failure('invalid_action', 400);
    }
    $options = settings();
    $secret = null;
    if ($action === 'save') {
        $groups = parse_groups($input['allowed_groups'] ?? '');
        if (is_wp_error($groups)) {
            return $groups;
        }
        $enabled = ($input['enabled'] ?? '') === '1';
        if ($enabled && (!$options['secret'] || !$groups)) {
            return failure('key_and_groups_required', 400);
        }
        $color = $input['binding_background_color'] ?? $options['binding_background_color'];
        $image = $input['binding_background_image'] ?? $options['binding_background_image'];
        if (!is_string($color) || !sanitize_hex_color($color)) {
            return failure('invalid_background_color', 400);
        }
        if (!is_string($image) || strlen($image) > 2048) {
            return failure('invalid_background_image', 400);
        }
        $image = trim($image);
        $parts = $image !== '' ? wp_parse_url($image) : array();
        if ($image !== '' && (
            !$parts || !in_array(strtolower($parts['scheme'] ?? ''), array('http', 'https'), true)
            || empty($parts['host']) || isset($parts['user']) || isset($parts['pass'])
            || preg_match('/[\s<>"\x00-\x1F\x7F]/', $image)
        )) {
            return failure('invalid_background_image', 400);
        }
        if (strlen($color) === 4) {
            $color = '#' . str_repeat($color[1], 2) . str_repeat($color[2], 2) . str_repeat($color[3], 2);
        }
        $options['binding_background_color'] = strtolower($color);
        $options['binding_background_image'] = esc_url_raw($image, array('http', 'https'));
        $options['enabled'] = $enabled;
        $options['allowed_groups'] = $groups;
        $options['allow_local_http'] = ($input['allow_local_http'] ?? '') === '1';
    } elseif ($action === 'rotate') {
        if (!$options['client_id']) {
            $options['client_id'] = 'az_' . bin2hex(random_bytes(16));
        }
        $options['key_id'] = bin2hex(random_bytes(8));
        $secret = bin2hex(random_bytes(32));
        $options['secret'] = $secret;
    } else {
        $options['enabled'] = false;
        $options['key_id'] = '';
        $options['secret'] = '';
    }
    update_option(OPTION, $options, false);
    if (settings() !== $options) {
        return failure('save_failed', 503);
    }
    return $secret;
}

function admin_page()
{
    if (!current_user_can('manage_options')) {
        wp_die(esc_html('无权访问此设置页。'));
    }
    $result = null;
    $revocations = null;
    $submitted = ($_SERVER['REQUEST_METHOD'] ?? '') === 'POST';
    if ($submitted) {
        if (in_array($_POST['az_action'] ?? '', array('revoke_one', 'revoke_batch'), true)) {
            $revocations = admin_revoke_bindings(wp_unslash($_POST));
        } else {
            $result = save_settings(wp_unslash($_POST));
        }
    }
    $options = settings();
    $theme = theme_status();
    echo '<div class="wrap"><h1>AstrBot 网站联动</h1>';
    echo '<p>群成员通过网站登录并在原群发送六位授权码后，可查询本人资料及签到；文章查询仅返回公开标题和链接。</p>';
    if ($submitted && $revocations === null) {
        if (is_wp_error($result)) {
            echo '<div class="notice notice-error"><p>保存失败：'
                . esc_html($result->get_error_code()) . '</p></div>';
        } else {
            echo '<div class="notice notice-success"><p>设置已保存。</p></div>';
        }
    }
    if (is_string($result)) {
        echo '<p><strong>新密钥仅在本次响应显示，请复制到 AstrBot 后台。旧密钥立即失效。</strong></p>';
        echo '<p><code>' . esc_html($result) . '</code></p>';
    }
    echo '<p>API：<code>' . esc_html(rest_url('astrbot-zibll/v1')) . '</code></p>';
    echo '<p>公开网站：<code>' . esc_html(untrailingslashit(home_url('/'))) . '</code></p>';
    echo '<p>用户撤销授权：<code>' . esc_html(add_query_arg('az_bindings', '1', home_url('/'))) . '</code></p>';
    echo '<p>Client ID：<code>' . esc_html($options['client_id'] ?: '尚未生成') . '</code></p>';
    echo '<p>Key ID：<code>' . esc_html($options['key_id'] ?: '尚未生成或已撤销') . '</code></p>';
    echo '<p>主题能力：' . ($theme['ready'] ? '可用' : '不可用')
        . '；站点时区：' . esc_html(wp_timezone_string())
        . '；签到：' . ($theme['checkin_enabled'] ? '开启' : '关闭')
        . '；积分：' . ($theme['points_enabled'] ? '开启' : '关闭')
        . '；等级：' . ($theme['level_enabled'] ? '开启' : '关闭') . '</p>';
    echo '<form method="post">';
    wp_nonce_field('astrbot_zibll_settings');
    echo '<p><label><input type="checkbox" name="enabled" value="1" '
        . checked($options['enabled'], true, false) . '>启用认证接口</label></p>';
    echo '<p><label for="az-groups">允许的群，每行 平台实例ID|机器人ID|群ID；空列表关闭业务</label></p>';
    echo '<textarea id="az-groups" name="allowed_groups" rows="5" class="large-text">'
        . esc_textarea(implode("\n", $options['allowed_groups'])) . '</textarea>';
    echo '<p><label><input type="checkbox" name="allow_local_http" value="1" '
        . checked($options['allow_local_http'], true, false)
        . '>允许本机 HTTP 调试（公开网站与请求主机都须为环回地址）</label></p>';
    echo '<h2>绑定页面外观</h2>';
    echo '<p><label for="az-background-color">背景色</label> '
        . '<input type="color" id="az-background-color" name="binding_background_color" value="'
        . esc_attr($options['binding_background_color']) . '"></p>';
    echo '<p><label for="az-background-image">背景图片地址（可选）</label></p>';
    echo '<input type="url" id="az-background-image" name="binding_background_image" class="large-text" maxlength="2048" value="'
        . esc_attr($options['binding_background_image']) . '" placeholder="https://example.com/background.jpg">';
    echo '<p class="description">可填写媒体库图片地址或可信图片直链，HTTPS 网站请使用 HTTPS 图片。留空不显示图片；图片无法加载时使用背景色。</p>';
    echo '<p><button class="button button-primary" name="az_action" value="save">保存设置</button> ';
    echo '<button class="button" name="az_action" value="rotate">生成 / 轮换密钥</button> ';
    echo '<button class="button" name="az_action" value="revoke">撤销密钥并停用</button></p>';
    echo '</form>';
    render_admin_bindings($revocations);
    echo '</div>';
}

function admin_revoke_bindings($input)
{
    global $wpdb;
    if (!current_user_can('manage_options')) {
        return failure('forbidden', 403);
    }
    if (!is_string($input['_wpnonce'] ?? null)
        || !wp_verify_nonce($input['_wpnonce'], 'astrbot_zibll_bindings')
    ) {
        return failure('invalid_nonce', 403);
    }
    $action = $input['az_action'] ?? '';
    $ids = $action === 'revoke_one' ? array($input['binding'] ?? '') : ($input['bindings'] ?? null);
    if (!in_array($action, array('revoke_one', 'revoke_batch'), true)
        || !is_array($ids) || !$ids || count($ids) > 50
    ) {
        return failure('invalid_parameters', 400);
    }
    $results = array();
    $admin = get_current_user_id();
    foreach ($ids as $hash) {
        if (!is_string($hash) || !preg_match('/\A[0-9a-f]{64}\z/', $hash)) {
            $results[] = array('id' => '', 'state' => 'invalid_parameters');
            continue;
        }
        $result = with_lock('actor:' . $hash, static function () use ($hash, $admin, $ids, $action, $wpdb) {
            return binding_transaction(static function () use ($hash, $admin, $ids, $action, $wpdb) {
                $row = binding_row($hash);
                $pending = $wpdb->get_var($wpdb->prepare(
                    'SELECT session_id FROM ' . binding_session_table() . ' WHERE active_actor = %s', $hash
                ));
                $state = !$row ? 'not_found' : (!$row['user_id'] && !$row['challenge_hash'] && !$pending ? 'already_revoked' : 'revoked');
                if ($row) {
                    clear_binding($hash);
                }
                binding_write($wpdb->insert(state_table(), array(
                    'state_key' => hash('sha256', 'revoke-audit:' . bin2hex(random_bytes(32))),
                    'expires_at' => time() + 7 * 86400,
                    'payload' => wp_json_encode(array(
                        'action' => $action, 'administrator' => $admin, 'at' => time(),
                        'target_count' => count($ids), 'binding' => $hash, 'result' => $state,
                    )),
                )));
                return $state;
            });
        });
        $results[] = array('id' => $hash, 'state' => is_wp_error($result) ? $result->get_error_code() : $result);
    }
    return $results;
}

function render_admin_bindings($result)
{
    global $wpdb;
    if (!current_user_can('manage_options')) {
        return;
    }
    $labels = array(
        'revoked' => '撤销成功', 'already_revoked' => '已撤销', 'not_found' => '找不到记录',
        'invalid_parameters' => '记录无效', 'busy' => '操作冲突，请稍后重试', 'unavailable' => '存储失败，未提交',
    );
    echo '<h2>用户授权管理</h2><p>仅撤销选中的群聊授权，不删除网站用户、内容、积分或签到历史。操作结果保留 7 天。</p>';
    if (is_wp_error($result)) {
        echo '<div class="notice notice-error"><p>' . esc_html($result->get_error_code()) . '</p></div>';
    } elseif (is_array($result)) {
        echo '<div class="notice notice-info"><ul>';
        foreach ($result as $item) {
            echo '<li>' . esc_html($item['id']) . '：' . esc_html($labels[$item['state']] ?? $item['state']) . '</li>';
        }
        echo '</ul></div>';
    }
    $filter = '';
    if (isset($_GET['az_filter']) && is_scalar($_GET['az_filter'])) {
        $filter = sanitize_text_field(wp_unslash((string) $_GET['az_filter']));
        $filter = function_exists('mb_substr') ? mb_substr($filter, 0, 100) : substr($filter, 0, 300);
    }
    $status = isset($_GET['az_status']) && is_scalar($_GET['az_status'])
        ? sanitize_key(wp_unslash((string) $_GET['az_status'])) : 'all';
    if (!in_array($status, array('all', 'bound', 'pending', 'revoked'), true)) {
        $status = 'all';
    }
    $where = array('1=1');
    $arguments = array();
    if ($filter !== '') {
        $like = '%' . $wpdb->esc_like($filter) . '%';
        $where[] = '(u.display_name LIKE %s OR u.user_login LIKE %s OR CAST(b.user_id AS CHAR) LIKE %s OR b.identity LIKE %s)';
        $arguments = array($like, $like, $like, $like);
    }
    if ($status === 'bound') {
        $where[] = 'b.user_id IS NOT NULL';
    } elseif ($status === 'pending') {
        $where[] = $wpdb->prepare('b.user_id IS NULL AND b.expires_at > %d', time());
    } elseif ($status === 'revoked') {
        $where[] = $wpdb->prepare('b.user_id IS NULL AND b.expires_at <= %d', time());
    }
    $where_sql = implode(' AND ', $where);
    $from_sql = ' FROM ' . binding_table() . ' b LEFT JOIN ' . $wpdb->users . ' u ON u.ID = b.user_id WHERE ' . $where_sql;
    $count_sql = 'SELECT COUNT(*)' . $from_sql;
    $total = $arguments
        ? (int) $wpdb->get_var($wpdb->prepare($count_sql, $arguments))
        : (int) $wpdb->get_var($count_sql);
    $page = max(1, is_scalar($_GET['az_page'] ?? 1) ? (int) ($_GET['az_page'] ?? 1) : 1);
    $page = min($page, max(1, (int) ceil($total / 20)));
    $rows_sql = 'SELECT b.*' . $from_sql . ' ORDER BY b.bound_at DESC, b.actor_hash LIMIT 20 OFFSET %d';
    $rows_arguments = array_merge($arguments, array(($page - 1) * 20));
    $rows = $wpdb->get_results($wpdb->prepare($rows_sql, $rows_arguments), ARRAY_A);
    echo '<form method="get" class="az-binding-filter">';
    echo '<input type="hidden" name="page" value="astrbot-zibll">';
    echo '<label for="az-binding-filter">筛选授权</label> ';
    echo '<input type="search" id="az-binding-filter" name="az_filter" value="' . esc_attr($filter)
        . '" placeholder="昵称、UID、平台、机器人、群或成员">';
    echo '<label for="az-binding-status">状态</label> <select id="az-binding-status" name="az_status">';
    foreach (array('all' => '全部', 'bound' => '已绑定', 'pending' => '待绑定', 'revoked' => '未授权') as $value => $label) {
        echo '<option value="' . esc_attr($value) . '"' . selected($status, $value, false) . '>' . esc_html($label) . '</option>';
    }
    echo '</select> <button class="button" type="submit">筛选</button> ';
    if ($filter !== '' || $status !== 'all') {
        echo '<a class="button" href="' . esc_url(admin_url('options-general.php?page=astrbot-zibll')) . '">清除</a>';
    }
    echo '</form>';
    echo '<form method="post" id="az-bulk-revoke">';
    wp_nonce_field('astrbot_zibll_bindings', '_wpnonce', false);
    echo '<button class="button" name="az_action" value="revoke_batch">撤销勾选授权</button>';
    echo '<table class="widefat striped"><thead><tr><th>选择</th><th>网站用户</th><th>平台 / 机器人 / 群 / 成员</th><th>绑定时间</th><th>状态</th><th>操作</th></tr></thead><tbody>';
    foreach ($rows as $row) {
        $user = $row['user_id'] ? get_userdata((int) $row['user_id']) : null;
        $identity = json_decode($row['identity'], true);
        echo '<tr><td><input type="checkbox" name="bindings[]" value="' . esc_attr($row['actor_hash']) . '" aria-label="选择授权"></td><td>'
            . esc_html($user ? $user->display_name : '—') . '<br>UID：' . (int) $row['user_id'] . '</td><td>';
        foreach (array('platform_id', 'bot_self_id', 'group_id', 'sender_id') as $field) {
            echo esc_html($identity[$field] ?? '') . '<br>';
        }
        echo '</td><td>' . esc_html($row['bound_at'] ? wp_date('Y-m-d H:i:s', (int) $row['bound_at']) : '未知 / 尚未绑定')
            . '</td><td>' . ($row['user_id'] ? '已绑定' : ($row['expires_at'] > time() ? '待绑定' : '未授权'))
            . '</td><td><button class="button" type="submit" form="az-one-' . esc_attr($row['actor_hash']) . '">撤销此条</button></td></tr>';
    }
    echo '</tbody></table></form>';
    foreach ($rows as $row) {
        echo '<form method="post" id="az-one-' . esc_attr($row['actor_hash']) . '">';
        wp_nonce_field('astrbot_zibll_bindings', '_wpnonce', false);
        echo '<input type="hidden" name="az_action" value="revoke_one"><input type="hidden" name="binding" value="'
            . esc_attr($row['actor_hash']) . '"></form>';
    }
    echo '<p>共 ' . $total . ' 条，第 ' . $page . ' 页 ';
    foreach (array($page - 1 => '上一页', $page + 1 => '下一页') as $target => $label) {
        if ($target > 0 && $target <= (int) ceil($total / 20)) {
            $page_args = array('page' => 'astrbot-zibll', 'az_page' => $target);
            if ($filter !== '') {
                $page_args['az_filter'] = $filter;
            }
            if ($status !== 'all') {
                $page_args['az_status'] = $status;
            }
            echo '<a class="button" href="' . esc_url(add_query_arg($page_args, admin_url('options-general.php'))) . '">'
                . esc_html($label) . '</a> ';
        }
    }
    echo '</p>';
}
