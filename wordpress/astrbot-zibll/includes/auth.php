<?php

namespace AstrBotZibll;

defined('ABSPATH') || exit;

const OPTION = 'astrbot_zibll_settings';
const PROTOCOL = 'AZ-HMAC-SHA256';
const NAMESPACE_PATH = '/astrbot-zibll/v1';
const CLOCK_WINDOW = 180;

function settings()
{
    $saved = get_option(OPTION, array());
    return array_merge(array(
        'enabled' => false,
        'client_id' => '',
        'key_id' => '',
        'secret' => '',
        'allowed_groups' => array(),
        'allow_local_http' => false,
        'binding_background_color' => '#fff0f5',
        'binding_background_image' => '',
    ), is_array($saved) ? $saved : array());
}

function failure($code, $status)
{
    return new \WP_Error($code, 'Bridge request rejected.', array('status' => $status));
}

function state_table()
{
    global $wpdb;
    return $wpdb->prefix . 'astrbot_zibll_state';
}

function signature($client, $key, $secret, $method, $route, $timestamp, $nonce, $body)
{
    $canonical = implode("\n", array(
        PROTOCOL, $client, $key, $method, $route, $timestamp, $nonce,
        hash('sha256', $body),
    ));
    return hash_hmac('sha256', $canonical, hex2bin($secret));
}

function loopback_host($host)
{
    if (!is_string($host)) {
        return false;
    }
    $host = strtolower(trim($host, '[]'));
    return $host === 'localhost' || $host === '::1'
        || (filter_var($host, FILTER_VALIDATE_IP, FILTER_FLAG_IPV4)
            && explode('.', $host)[0] === '127');
}

function transport_allowed($options)
{
    if (is_ssl()) {
        return true;
    }
    $public_host = wp_parse_url(home_url('/'), PHP_URL_HOST);
    $request_host = wp_parse_url('http://' . ($_SERVER['HTTP_HOST'] ?? ''), PHP_URL_HOST);
    return $options['allow_local_http'] === true
        && loopback_host($public_host) && loopback_host($request_host);
}

function validate_signature($request, $options, $now)
{
    $client = $request->get_header('x-az-client');
    $key = $request->get_header('x-az-key');
    $timestamp = $request->get_header('x-az-time');
    $nonce = $request->get_header('x-az-nonce');
    $provided = $request->get_header('x-az-signature');
    if ($options['enabled'] !== true
        || !is_string($client) || !is_string($key) || !is_string($timestamp)
        || !is_string($nonce) || !is_string($provided)
        || !preg_match('/\A[A-Za-z0-9_-]{1,64}\z/', $client)
        || !preg_match('/\A[A-Za-z0-9_-]{1,64}\z/', $key)
        || !preg_match('/\A[0-9]{10}\z/', $timestamp)
        || !preg_match('/\A[0-9a-f]{32}\z/', $nonce)
        || !preg_match('/\A[0-9a-f]{64}\z/', $provided)
        || !is_string($options['secret'])
        || !preg_match('/\A[0-9a-f]{64}\z/', $options['secret'])
        || $client !== $options['client_id'] || $key !== $options['key_id']
        || abs($now - (int) $timestamp) > CLOCK_WINDOW
    ) {
        return failure('authentication_failed', 401);
    }
    $expected = signature(
        $client, $key, $options['secret'], $request->get_method(),
        $request->get_route(), $timestamp, $nonce, $request->get_body()
    );
    if (!hash_equals($expected, $provided)) {
        return failure('authentication_failed', 401);
    }
    $query = $request->get_query_params();
    if (array_diff(array_keys($query), array('rest_route'))
        || (array_key_exists('rest_route', $query) && $query['rest_route'] !== $request->get_route())
    ) {
        return failure('invalid_parameters', 400);
    }
    return true;
}

function consume_nonce($client, $key, $nonce, $now)
{
    global $wpdb;
    $table = state_table();
    $digest = hash('sha256', implode("\n", array('nonce', $client, $key, $nonce)));
    $previous = $wpdb->suppress_errors(true);
    try {
        $inserted = $wpdb->query($wpdb->prepare(
            "INSERT IGNORE INTO $table (state_key, expires_at) VALUES (%s, %d)",
            $digest, $now + 2 * CLOCK_WINDOW + 1
        ));
        if ($inserted === false) {
            return failure('unavailable', 503);
        }
        if ($inserted !== 1) {
            return failure('replay', 409);
        }
        // A future-dated signature remains valid for two full clock windows.
        $wpdb->query($wpdb->prepare(
            "DELETE FROM $table WHERE expires_at < %d LIMIT 100", $now
        ));
        return true;
    } finally {
        $wpdb->suppress_errors($previous);
    }
}

function authorize($request)
{
    $options = settings();
    if (!transport_allowed($options)) {
        return failure('secure_transport_required', 403);
    }
    $now = time();
    $valid = validate_signature($request, $options, $now);
    if (is_wp_error($valid)) {
        return $valid;
    }
    $capabilities = $request->get_method() === 'GET'
        && $request->get_route() === NAMESPACE_PATH . '/capabilities'
        && $request->get_body() === '';
    $action = substr($request->get_route(), strlen(NAMESPACE_PATH) + 1);
    if (!$capabilities) {
        if ($request->get_method() !== 'POST' || !in_array($action, business_routes(), true)
            || strlen($request->get_body()) > 8192 || !$request->is_json_content_type()
        ) {
            return failure('invalid_parameters', 400);
        }
        $input = $request->get_json_params();
        $fields = array(
            'binding/start' => array(), 'binding/status' => array(),
            'binding/confirm' => array('expected_uid', 'challenge_id'),
            'binding/revoke' => array('confirm'), 'profile' => array(),
            'binding/code/start' => array('request_id', 'event_time'),
            'binding/code/redeem' => array('session_id', 'code', 'request_id', 'event_time'),
            'binding/code/status' => array('session_id', 'request_id'),
            'checkin' => array('request_id', 'event_time'),
            'checkin/status' => array('request_id', 'event_time'),
            'articles/latest' => array('count'), 'articles/search' => array('count', 'keyword'),
        );
        if (!is_array($input) || array_diff(array_keys($input), array_merge(array('actor'), $fields[$action]))) {
            return failure('invalid_parameters', 400);
        }
        $actor = actor_data($request);
        if (is_wp_error($actor)) {
            return $actor;
        }
    }
    $consumed = consume_nonce(
        $options['client_id'], $options['key_id'], $request->get_header('x-az-nonce'), $now
    );
    if (is_wp_error($consumed) || $capabilities) {
        return $consumed;
    }
    $limit = rate_limit('group:' . $actor['scope_hash'], 30, 60);
    if (is_wp_error($limit)) {
        return $limit;
    }
    return rate_limit('actor:' . $actor['actor_hash'], 10, 30);
}
