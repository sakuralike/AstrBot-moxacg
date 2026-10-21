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
    $submitted = ($_SERVER['REQUEST_METHOD'] ?? '') === 'POST';
    if ($submitted) {
        $result = save_settings(wp_unslash($_POST));
    }
    $options = settings();
    $theme = theme_status();
    echo '<div class="wrap"><h1>AstrBot 网站联动</h1>';
    echo '<p>群成员通过网站登录授权并在原群确认后，可查询本人资料及签到；文章查询仅返回公开标题和链接。</p>';
    if ($submitted) {
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
    echo '<p><button class="button button-primary" name="az_action" value="save">保存设置</button> ';
    echo '<button class="button" name="az_action" value="rotate">生成 / 轮换密钥</button> ';
    echo '<button class="button" name="az_action" value="revoke">撤销密钥并停用</button></p>';
    echo '</form></div>';
}
