<?php
/**
 * Plugin Name: AstrBot Zibll Bridge
 * Plugin URI: https://moxacg.moe/
 * Description: AstrBot 与 Zibll 的账号授权、本人资料、签到及公开文章桥接。
 * Version: 0.0.3
 * Author: moxacg
 * Author URI: https://moxacg.moe/
 * Requires PHP: 8.0
 */

namespace AstrBotZibll;

defined('ABSPATH') || exit;

require_once __DIR__ . '/includes/auth.php';
require_once __DIR__ . '/includes/admin.php';
require_once __DIR__ . '/includes/binding.php';
require_once __DIR__ . '/includes/binding-code.php';
require_once __DIR__ . '/includes/business.php';
require_once __DIR__ . '/includes/checkin.php';
require_once __DIR__ . '/includes/points.php';
require_once __DIR__ . '/includes/blindbox.php';

register_activation_hook(__FILE__, __NAMESPACE__ . '\\install');
add_action('plugins_loaded', __NAMESPACE__ . '\\maybe_install');
add_action('admin_menu', __NAMESPACE__ . '\\admin_menu');
add_action('rest_api_init', __NAMESPACE__ . '\\register_routes');
add_filter('rest_post_dispatch', __NAMESPACE__ . '\\private_response', 10, 3);
add_action('template_redirect', __NAMESPACE__ . '\\binding_page', -100);
add_action('wp_ajax_user_checkin', __NAMESPACE__ . '\\website_checkin_guard', -100);
add_action('deleted_user', __NAMESPACE__ . '\\remove_user_bindings');

function install()
{
    global $wpdb;
    require_once ABSPATH . 'wp-admin/includes/upgrade.php';
    $table = state_table();
    $collate = $wpdb->get_charset_collate();
    dbDelta("CREATE TABLE $table (
        state_key char(64) NOT NULL,
        expires_at bigint(20) unsigned NOT NULL,
        hits int(10) unsigned NOT NULL DEFAULT 0,
        payload text DEFAULT NULL,
        PRIMARY KEY  (state_key),
        KEY expires_at (expires_at)
    ) $collate;");
    $bindings = binding_table();
    dbDelta("CREATE TABLE $bindings (
        actor_hash char(64) NOT NULL,
        scope_hash char(64) NOT NULL,
        identity text NOT NULL,
        user_id bigint(20) unsigned DEFAULT NULL,
        candidate_id bigint(20) unsigned DEFAULT NULL,
        challenge_hash char(64) DEFAULT NULL,
        expires_at bigint(20) unsigned NOT NULL DEFAULT 0,
        revoke_until bigint(20) unsigned NOT NULL DEFAULT 0,
        bound_at bigint(20) unsigned NOT NULL DEFAULT 0,
        PRIMARY KEY  (actor_hash),
        UNIQUE KEY scope_user (scope_hash,user_id),
        UNIQUE KEY challenge_hash (challenge_hash),
        KEY user_id (user_id)
    ) $collate;");
    $daily = daily_table();
    dbDelta("CREATE TABLE $daily (
        user_id bigint(20) unsigned NOT NULL,
        site_day date NOT NULL,
        state varchar(24) NOT NULL,
        PRIMARY KEY  (user_id,site_day)
    ) $collate;");
    $operations = operation_table();
    dbDelta("CREATE TABLE $operations (
        request_key char(64) NOT NULL,
        input_hash char(64) NOT NULL,
        user_id bigint(20) unsigned NOT NULL,
        site_day date NOT NULL,
        state varchar(24) NOT NULL,
        expires_at bigint(20) unsigned NOT NULL,
        PRIMARY KEY  (request_key),
        KEY expires_at (expires_at)
    ) $collate;");
    install_binding_codes($collate);
    install_points($collate);
    install_blindbox($collate);
    add_option(OPTION, settings(), '', false);
    if (binding_schema_ready() && points_schema_ready()) {
        update_option('astrbot_zibll_schema', 'points_v1', false);
    }
}

function maybe_install()
{
    if (get_option('astrbot_zibll_schema') !== 'points_v1') {
        install();
    }
    if (get_option('astrbot_zibll_blindbox_schema') !== 'blindbox_v1'
        || !blindbox_schema_ready()
    ) {
        global $wpdb;
        require_once ABSPATH . 'wp-admin/includes/upgrade.php';
        install_blindbox($wpdb->get_charset_collate());
        if (blindbox_schema_ready()) {
            update_option('astrbot_zibll_blindbox_schema', 'blindbox_v1', false);
        }
    }
}

function register_routes()
{
    register_rest_route('astrbot-zibll/v1', '/capabilities', array(
        'methods' => 'GET',
        'permission_callback' => __NAMESPACE__ . '\\authorize',
        'callback' => __NAMESPACE__ . '\\capabilities',
    ));
    foreach (business_routes() as $route) {
        register_rest_route('astrbot-zibll/v1', '/' . $route, array(
            'methods' => 'POST',
            'permission_callback' => __NAMESPACE__ . '\\authorize',
            'callback' => __NAMESPACE__ . '\\dispatch_business',
        ));
    }
}

function remove_user_bindings($uid)
{
    remove_user_code_bindings((int) $uid);
}

function theme_status()
{
    $required = array(
        '_pz', 'zib_get_user_avatar_url', 'zib_get_user_post_count',
        'get_user_comment_count', 'zibpay_get_user_points', 'zibpay_update_user_points', 'zib_user_checkin',
        'zib_get_user_the_checkin_details', 'zib_get_user_checkin_all_day',
        'zib_get_user_checkin_continuous_day',
        'zibpay_user_is_allow_add_free_points', 'zib_user_is_allow_add_integral',
        'zib_get_user_meta', 'zibpay_get_user_today_free_points',
        'zib_get_user_today_integral', 'zib_get_user_points_add_options',
    );
    $ready = wp_get_theme()->get_template() === 'zibll';
    foreach ($required as $function) {
        $ready = $ready && function_exists($function);
    }
    return array(
        'ready' => $ready,
        'checkin_enabled' => $ready && (bool) _pz('checkin_s'),
        'points_enabled' => $ready && (bool) _pz('points_s'),
        'level_enabled' => $ready && (bool) _pz('user_level_s', true),
    );
}

function capabilities()
{
    $theme = theme_status();
    if (!$theme['ready']) {
        return failure('unavailable', 503);
    }
    $points_available = $theme['points_enabled'] && points_schema_ready() && points_transactions_ready();
    $blindbox_ready = blindbox_schema_ready() && blindbox_transactions_ready();
    return new \WP_REST_Response(array(
        'request_id' => wp_generate_uuid4(),
        'code' => 'ok',
        'message' => 'Authenticated connection available.',
        'data' => array(
            'protocol' => PROTOCOL,
            'public_site_url' => untrailingslashit(home_url('/')),
            'timezone' => wp_timezone_string(),
            'actions' => array_merge(array('capabilities'), business_routes()),
            'binding_code' => binding_schema_ready() && binding_transactions_ready(),
            'points_management' => $points_available,
            'points_actions' => $points_available
                ? array('points/add', 'points/subtract', 'points/status') : array(),
            'points_max_amount' => POINTS_MAX_AMOUNT,
            'blindbox' => $blindbox_ready,
            'blindbox_actions' => $blindbox_ready
                ? array('blindbox/open', 'blindbox/exchange', 'blindbox/status') : array(),
            'management_url' => add_query_arg('az_bindings', '1', home_url('/')),
            'theme' => $theme,
        ),
    ));
}

function private_response($response, $server, $request)
{
    if (stripos($request->get_route(), NAMESPACE_PATH . '/') !== 0) {
        return $response;
    }
    $response->header('Cache-Control', 'no-store, private, max-age=0');
    $response->header('Pragma', 'no-cache');
    $response->header('Expires', '0');
    if ($response->is_error()) {
        $data = $response->get_data();
        $response->set_data(array(
            'request_id' => wp_generate_uuid4(),
            'code' => $data['code'] ?? 'request_failed',
            'message' => 'Bridge request rejected.',
            'data' => null,
        ));
    }
    return $response;
}
