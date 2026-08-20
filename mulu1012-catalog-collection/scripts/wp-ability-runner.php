<?php
/**
 * WP-CLI bridge used by wp_ability.py.
 *
 * @package Mulu1012_Catalog_Collection
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit( 1 );
}

$allowed = array(
	'mulu1012-catalog/deduplicate-candidates',
	'mulu1012-catalog/import-local-batch',
	'mulu1012-catalog/get-batch',
	'mulu1012-catalog/get-candidate',
);

$runner_args  = isset( $args ) && is_array( $args ) ? array_values( $args ) : array();
$ability_name = isset( $runner_args[0] ) ? (string) $runner_args[0] : '';
$input_path   = isset( $runner_args[1] ) ? (string) $runner_args[1] : '';
$output_path  = isset( $runner_args[2] ) ? (string) $runner_args[2] : '';
$user_ref     = isset( $runner_args[3] ) ? (string) $runner_args[3] : '';

$write = static function ( array $value ) use ( $output_path ) {
	if ( '' === $output_path ) {
		return false;
	}
	return false !== file_put_contents(
		$output_path,
		wp_json_encode( $value, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PRETTY_PRINT ) . PHP_EOL,
		LOCK_EX
	);
};

$fail = static function ( $message, $code = 'mulu1012_ability_runner_error', $data = null ) use ( $write ) {
	$write(
		array(
			'ok'    => false,
			'error' => array(
				'code'    => (string) $code,
				'message' => (string) $message,
				'data'    => $data,
			),
		)
	);
	exit( 1 );
};

if ( ! in_array( $ability_name, $allowed, true ) ) {
	$fail( 'Ability is not allowed by the catalog Skill.', 'mulu1012_ability_not_allowed' );
}
if ( '' === $input_path || ! is_readable( $input_path ) ) {
	$fail( 'Ability input JSON is unavailable.', 'mulu1012_ability_input_unavailable' );
}
if ( '' === $output_path ) {
	exit( 1 );
}

$input = json_decode( file_get_contents( $input_path ), true );
if ( JSON_ERROR_NONE !== json_last_error() ) {
	$fail( 'Ability input is not valid JSON.', 'mulu1012_ability_invalid_json', json_last_error_msg() );
}
if ( ! function_exists( 'wp_get_ability' ) ) {
	$fail( 'WordPress Abilities API is unavailable.', 'mulu1012_abilities_unavailable' );
}
if ( '' === $user_ref ) {
	$fail( 'A WordPress user login or ID is required.', 'mulu1012_ability_user_required' );
}

$user = false;
if ( 0 === strpos( $user_ref, 'id:' ) ) {
	$user = get_user_by( 'id', (int) substr( $user_ref, 3 ) );
} elseif ( 0 === strpos( $user_ref, 'login:' ) ) {
	$user = get_user_by( 'login', substr( $user_ref, 6 ) );
} elseif ( ctype_digit( $user_ref ) ) {
	$user = get_user_by( 'id', (int) $user_ref );
	if ( false === $user ) {
		$user = get_user_by( 'login', $user_ref );
	}
} else {
	$user = get_user_by( 'login', $user_ref );
	if ( false === $user ) {
		$user = get_user_by( 'email', $user_ref );
	}
}
if ( false === $user ) {
	$fail( 'The requested WordPress user does not exist.', 'mulu1012_ability_user_invalid' );
}
wp_set_current_user( (int) $user->ID );

$ability = wp_get_ability( $ability_name );
if ( ! is_object( $ability ) || ! is_callable( array( $ability, 'execute' ) ) ) {
	$fail( 'The requested Ability is not registered.', 'mulu1012_ability_unavailable' );
}

try {
	$result = $ability->execute( $input );
	if ( is_wp_error( $result ) ) {
		$fail(
			$result->get_error_message(),
			$result->get_error_code(),
			$result->get_error_data()
		);
	}
	if ( ! $write( array( 'ok' => true, 'ability' => $ability_name, 'result' => $result ) ) ) {
		exit( 1 );
	}
} catch ( Throwable $error ) {
	$fail(
		$error->getMessage(),
		'mulu1012_ability_exception',
		array( 'exception' => get_class( $error ) )
	);
}
