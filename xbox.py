import pygame
import time

# Initialize Pygame and joystick module
pygame.init()
pygame.joystick.init()

def get_controller_state(joystick):
    pygame.event.pump()  # Process event queue

    state = {
        'axes': [joystick.get_axis(i) for i in range(joystick.get_numaxes())],
        'buttons': [joystick.get_button(i) for i in range(joystick.get_numbuttons())],
        'hats': [joystick.get_hat(i) for i in range(joystick.get_numhats())]
    }
    return state

def coordinates(joystick):
    state = get_controller_state(joystick)
    axes = state['axes']
    if len(axes) >= 2:
        return (axes[0], axes[1])  # Typically left stick X, Y
    return (0.0, 0.0)

def main():
    # Wait for at least one joystick
    while pygame.joystick.get_count() == 0:
        print("Waiting for controller...")
        pygame.joystick.quit()
        pygame.joystick.init()
        time.sleep(1)

    # Initialize the first controller
    joystick = pygame.joystick.Joystick(0)
    joystick.init()
    print(f"Detected controller: {joystick.get_name()}")

    try:
        while True:
            state = get_controller_state(joystick)
            print("Axes:", state['axes'])
            print("Buttons:", state['buttons'])
            print("D-pad:", state['hats'])
            print("Left Stick Coordinates:", coordinates(joystick))
            print('-' * 40)
            time.sleep(1)  # polling interval (1 second)

    except KeyboardInterrupt:
        print("Exiting...")
    finally:
        pygame.quit()

if __name__ == '__main__':
    main()
